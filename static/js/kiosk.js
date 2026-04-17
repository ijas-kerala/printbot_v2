/**
 * static/js/kiosk.js — Kiosk SSE client and Dot mascot state machine.
 *
 * Responsibilities:
 *   - Connect to GET /kiosk/events via EventSource
 *   - On each 'status' event: call setMascotState() and update headline + desc text
 *   - Auto-clear 'completed' state back to 'idle' after 30 seconds of silence
 *   - Show a 'Reconnecting...' overlay on connection loss; reconnect after 3s
 *   - Hidden admin tap zone: 5 rapid taps on #admin-tap → /admin/login
 *
 * Depends on: mascot.js (setMascotState)
 * No other external dependencies. Uses textContent exclusively (never innerHTML).
 */

(function () {
  'use strict';

  // ── DOM references ──────────────────────────────────────────────────────────
  var mascotEl      = document.querySelector('.dot-mascot');
  var headlineEl    = document.getElementById('kiosk-headline');
  var descEl        = document.getElementById('kiosk-desc');
  var reconnOverlay = document.getElementById('reconnect-overlay');
  var adminTap      = document.getElementById('admin-tap');

  // ── State map ───────────────────────────────────────────────────────────────
  // Each entry: { mascot: string, headline: string, desc: string }
  var STATE_MAP = {
    idle: {
      mascot:   'idle',
      headline: 'Ready to print',
      desc:     'Scan the QR code to start',
    },
    uploading: {
      mascot:   'working',
      headline: 'Receiving document',
      desc:     'Someone is uploading right now',
    },
    payment_pending: {
      mascot:   'waiting',
      headline: 'Waiting for payment',
      desc:     '',
    },
    printing: {
      mascot:   'working',
      headline: 'Printing now',
      desc:     'Collect from the output tray',
    },
    completed: {
      mascot:   'happy',
      headline: 'Print complete',
      desc:     'Collect your pages below',
    },
    error: {
      mascot:   'sad',
      headline: 'Printer unavailable',
      desc:     'Please contact staff',
    },
  };

  // ── Internal state ──────────────────────────────────────────────────────────
  var currentState        = 'idle';
  var completedClearTimer = null;
  var eventSource         = null;
  var reconnectTimer      = null;

  // ── State machine ───────────────────────────────────────────────────────────

  /**
   * Transition the kiosk display to a new state.
   * Updates the Dot mascot, headline, and description.
   *
   * @param {string} state - A key from STATE_MAP
   */
  function updateState(state) {
    if (state === currentState && state !== 'completed') return;

    currentState = state;

    // Clear any pending completed→idle auto-reset timer
    if (completedClearTimer !== null) {
      clearTimeout(completedClearTimer);
      completedClearTimer = null;
    }

    var entry = STATE_MAP[state] || STATE_MAP.idle;

    // Update Dot mascot animation state
    setMascotState(mascotEl, entry.mascot);

    // Update text — textContent only, no user content involved
    headlineEl.textContent = entry.headline;
    descEl.textContent     = entry.desc;

    // Auto-reset from completed back to idle after 30 seconds of no new events
    if (state === 'completed') {
      completedClearTimer = setTimeout(function () {
        completedClearTimer = null;
        if (currentState === 'completed') {
          updateState('idle');
        }
      }, 30000);
    }
  }

  // ── SSE connection ──────────────────────────────────────────────────────────

  function connect() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

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
        // Malformed JSON — not expected but degrade gracefully
        return;
      }

      var state = (typeof data.state === 'string') ? data.state : 'idle';
      updateState(state);
    });

    eventSource.onerror = function () {
      // Show reconnecting overlay — set only desc, leave headline unchanged
      reconnOverlay.classList.add('visible');
      descEl.textContent = 'Reconnecting...';

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

  // ── Hidden admin tap zone ───────────────────────────────────────────────────
  // 5 taps within 2 seconds on #admin-tap navigates to /admin/login

  var tapCount      = 0;
  var tapResetTimer = null;

  function handleAdminTap() {
    tapCount += 1;

    if (tapResetTimer !== null) {
      clearTimeout(tapResetTimer);
    }
    tapResetTimer = setTimeout(function () {
      tapCount      = 0;
      tapResetTimer = null;
    }, 2000);

    if (tapCount >= 5) {
      tapCount = 0;
      clearTimeout(tapResetTimer);
      tapResetTimer = null;
      window.location.href = '/admin/login';
    }
  }

  adminTap.addEventListener('click', handleAdminTap);

  // Prevent double-counting the synthetic click that follows touchend on mobile
  adminTap.addEventListener('touchend', function (evt) {
    evt.preventDefault();
    handleAdminTap();
  });

  // ── Boot ────────────────────────────────────────────────────────────────────
  connect();

}());
