/**
 * static/js/kiosk.js — Kiosk SSE client and status state machine.
 *
 * Responsibilities:
 *   - Connect to GET /kiosk/events via EventSource
 *   - On each 'status' event: update the right-panel state colours, icon, and text
 *   - Auto-clear the 'completed' state back to 'idle' after 30 seconds of silence
 *   - Show a 'Reconnecting...' overlay on connection loss; reconnect after 3s
 *   - Hidden admin trigger: 5 rapid taps on #admin-trigger → /admin/login
 *
 * No external dependencies. Uses textContent exclusively (never innerHTML).
 */

(function () {
  'use strict';

  // ── DOM references ──────────────────────────────────────────────────────────
  var panel         = document.getElementById('status-panel');
  var iconEl        = document.getElementById('status-icon');
  var textEl        = document.getElementById('status-text');
  var queueEl       = document.getElementById('queue-info');
  var reconnOverlay = document.getElementById('reconnect-overlay');
  var adminTrigger  = document.getElementById('admin-trigger');

  // ── State constants ─────────────────────────────────────────────────────────
  var ALL_STATE_CLASSES = [
    'state-idle',
    'state-uploading',
    'state-payment_pending',
    'state-printing',
    'state-completed',
    'state-error',
  ];

  var STATE_TEXT = {
    idle:            'Ready to print',
    uploading:       'Receiving files...',
    payment_pending: 'Waiting for payment...',
    printing:        'Printing in progress...',
    completed:       'Print complete!',
    error:           'Printer offline',
  };

  // SVG icon markup per state. Using textContent is not applicable for SVG
  // injection — these strings are pre-defined, server-controlled static assets
  // with no user data in them, so outerHTML assignment is acceptable here.
  // SECURITY: These strings contain no user-supplied content.
  var STATE_ICONS = {
    idle: '<svg viewBox="0 0 24 24" fill="none" stroke="#F5F0EB" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',

    uploading: '<svg viewBox="0 0 24 24" fill="none" stroke="#FDE68A" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>',

    payment_pending: '<svg viewBox="0 0 24 24" fill="none" stroke="#FED7AA" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',

    printing: '<svg viewBox="0 0 24 24" fill="none" stroke="#93C5FD" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>',

    completed: '<svg viewBox="0 0 24 24" fill="none" stroke="#86EFAC" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',

    error: '<svg viewBox="0 0 24 24" fill="none" stroke="#FCA5A5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
  };

  // ── Internal state ──────────────────────────────────────────────────────────
  var currentState        = 'idle';
  var completedClearTimer = null;   // setTimeout handle for auto-clearing 'completed'
  var eventSource         = null;   // Active EventSource instance
  var reconnectTimer      = null;   // setTimeout handle for reconnection delay

  // ── State machine ───────────────────────────────────────────────────────────

  /**
   * Transition the right panel to a new visual state.
   * @param {string} state   - One of the STATE_TEXT keys
   * @param {number} queueLen - Number of jobs waiting behind the current one
   */
  function updateState(state, queueLen) {
    if (state === currentState && state !== 'completed') {
      // Only re-render if the queue depth changed, to avoid unnecessary DOM churn
      updateQueueText(state, queueLen);
      return;
    }

    currentState = state;

    // Clear any pending completed→idle timer
    if (completedClearTimer !== null) {
      clearTimeout(completedClearTimer);
      completedClearTimer = null;
    }

    // Swap state class on the panel
    ALL_STATE_CLASSES.forEach(function (cls) {
      panel.classList.remove(cls);
    });
    panel.classList.add('state-' + state);

    // Update icon (SECURITY: static lookup, no user content)
    var iconSvg = STATE_ICONS[state] || STATE_ICONS.idle;
    iconEl.innerHTML = iconSvg;  // Safe: only static SVG strings defined above

    // Update status text
    textEl.textContent = STATE_TEXT[state] || 'Ready to print';

    // Update queue info
    updateQueueText(state, queueLen);

    // Schedule auto-clear for completed state (30s)
    if (state === 'completed') {
      completedClearTimer = setTimeout(function () {
        completedClearTimer = null;
        // Only revert if no new state event arrived in the meantime
        if (currentState === 'completed') {
          updateState('idle', 0);
        }
      }, 30000);
    }
  }

  /**
   * Update the secondary queue-info line below the status text.
   * Hidden when empty so there is no stray empty line.
   */
  function updateQueueText(state, queueLen) {
    var text = '';

    if (state === 'printing' && queueLen > 0) {
      text = queueLen === 1
        ? '1 job waiting'
        : queueLen + ' jobs waiting';
    } else if (state === 'printing') {
      text = '';
    }
    // All other states: leave blank

    queueEl.textContent = text;
  }

  // ── SSE connection ──────────────────────────────────────────────────────────

  function connect() {
    // Clear any pending reconnect timer
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    // Close previous connection if still open
    if (eventSource !== null) {
      eventSource.close();
      eventSource = null;
    }

    eventSource = new EventSource('/kiosk/events');

    eventSource.addEventListener('status', function (evt) {
      // Hide the reconnecting overlay on first successful event
      reconnOverlay.classList.remove('visible');

      var data;
      try {
        data = JSON.parse(evt.data);
      } catch (e) {
        // Malformed JSON from server — not expected but handle gracefully
        return;
      }

      var state    = (typeof data.state === 'string') ? data.state : 'idle';
      var queueLen = (typeof data.queue_length === 'number') ? data.queue_length : 0;
      updateState(state, queueLen);
    });

    eventSource.onerror = function () {
      // Show reconnecting overlay
      reconnOverlay.classList.add('visible');

      // Close the broken connection and schedule a fresh one
      if (eventSource !== null) {
        eventSource.close();
        eventSource = null;
      }

      // EDGE CASE: avoid stacking multiple reconnect timers
      if (reconnectTimer === null) {
        reconnectTimer = setTimeout(function () {
          reconnectTimer = null;
          connect();
        }, 3000);
      }
    };
  }

  // ── Hidden admin trigger ────────────────────────────────────────────────────

  var tapCount      = 0;
  var tapResetTimer = null;

  adminTrigger.addEventListener('click', function () {
    tapCount += 1;

    // Reset counter if no further tap arrives within 3 seconds
    if (tapResetTimer !== null) {
      clearTimeout(tapResetTimer);
    }
    tapResetTimer = setTimeout(function () {
      tapCount = 0;
      tapResetTimer = null;
    }, 3000);

    if (tapCount >= 5) {
      tapCount = 0;
      clearTimeout(tapResetTimer);
      tapResetTimer = null;
      window.location.href = '/admin/login';
    }
  });

  // Also handle touch events on the touchscreen kiosk
  adminTrigger.addEventListener('touchend', function (evt) {
    // Prevent the synthetic 'click' that follows touchend so we don't double-count
    evt.preventDefault();
    tapCount += 1;

    if (tapResetTimer !== null) {
      clearTimeout(tapResetTimer);
    }
    tapResetTimer = setTimeout(function () {
      tapCount = 0;
      tapResetTimer = null;
    }, 3000);

    if (tapCount >= 5) {
      tapCount = 0;
      clearTimeout(tapResetTimer);
      tapResetTimer = null;
      window.location.href = '/admin/login';
    }
  });

  // ── Boot ────────────────────────────────────────────────────────────────────
  connect();

}());
