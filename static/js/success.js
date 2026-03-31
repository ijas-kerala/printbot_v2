/**
 * static/js/success.js — Post-payment job status polling.
 *
 * Responsibilities:
 *  - Poll GET /api/status/<job_id> every 3 seconds.
 *  - Update the status card UI per state.
 *  - Show coupon code on failure.
 *  - Stop polling on terminal states (completed / failed / expired).
 *  - Show "Print another" CTA once terminal.
 *
 * States: paid | processing | printing | completed | failed | expired
 */

(function () {
  'use strict';

  // ── Read embedded data ────────────────────────────────────────────────────────
  var dataEl = document.getElementById('success-data');
  if (!dataEl) { return; }
  var data;
  try { data = JSON.parse(dataEl.textContent); }
  catch (e) { return; }

  var jobId         = data.job_id;
  var currentStatus = data.initial_status;

  // ── DOM references ────────────────────────────────────────────────────────────
  var statusRing     = document.getElementById('status-ring');
  var statusIcon     = document.getElementById('status-icon');
  var statusHeader   = document.getElementById('status-header');
  var statusHeadline = document.getElementById('status-headline');
  var statusSub      = document.getElementById('status-sub');
  var pulseDots      = document.getElementById('pulse-dots');
  var detailStatus   = document.getElementById('detail-status');
  var detailCupsRow  = document.getElementById('detail-cups-row');
  var detailCups     = document.getElementById('detail-cups');
  var couponSection  = document.getElementById('coupon-section');
  var couponCode     = document.getElementById('coupon-code');
  var pollStatus     = document.getElementById('poll-status');
  var terminalActions= document.getElementById('terminal-actions');

  // ── State config ──────────────────────────────────────────────────────────────

  var TERMINAL_STATES = ['completed', 'failed', 'expired'];

  var STATE_UI = {
    paid: {
      theme:    'is-waiting',
      ring:     'status-ring-waiting',
      headline: 'Payment confirmed',
      sub:      'Your job is queued and will print shortly.',
      dots:     true,
      icon:     '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    },
    processing: {
      theme:    'is-waiting',
      ring:     'status-ring-waiting',
      headline: 'Preparing your print…',
      sub:      'Applying your page settings and layout.',
      dots:     true,
      icon:     '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    },
    printing: {
      theme:    'is-printing',
      ring:     'status-ring-printing',
      headline: 'Printing now',
      sub:      'Your document is being printed. Please wait nearby.',
      dots:     true,
      dotsClass: 'printing',
      icon:     '<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
    },
    completed: {
      theme:    'is-done',
      ring:     'status-ring-success',
      headline: 'Print complete!',
      sub:      'Collect your document from the printer tray.',
      dots:     false,
      icon:     '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    },
    failed: {
      theme:    'is-error',
      ring:     'status-ring-error',
      headline: 'Print failed',
      sub:      'Something went wrong. A refund coupon has been issued below.',
      dots:     false,
      icon:     '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    },
    expired: {
      theme:    'is-error',
      ring:     'status-ring-error',
      headline: 'Job expired',
      sub:      'This job was not completed in time. Please start a new print.',
      dots:     false,
      icon:     '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    },
  };

  // FALLBACK: unknown status treated as waiting
  function getUI(status) {
    return STATE_UI[status] || STATE_UI.paid;
  }

  // ── UI updater ───────────────────────────────────────────────────────────────

  function applyUI(status, jobData) {
    var ui = getUI(status);

    // Header theme
    statusHeader.className = 'success-header ' + ui.theme;
    statusHeadline.className = 'status-headline ' + ui.theme;
    statusHeadline.textContent = ui.headline;
    statusSub.textContent  = ui.sub;

    // Pulse dots
    pulseDots.style.display = ui.dots ? '' : 'none';
    if (ui.dotsClass) {
      pulseDots.className = 'pulse-dots ' + ui.dotsClass;
    } else {
      pulseDots.className = 'pulse-dots';
    }

    // Status ring
    statusRing.className = 'status-ring ' + ui.ring;

    // Icon — SECURITY: static SVG strings, no user content
    statusIcon.innerHTML = ui.icon;

    // Add spinning class for printing
    if (status === 'printing') {
      statusIcon.classList.add('spin-icon');
    } else {
      statusIcon.classList.remove('spin-icon');
    }

    // Badge in detail row
    var badgeClass = 'badge badge-' + status;
    var badgeEl = document.createElement('span');
    badgeEl.className = badgeClass;
    badgeEl.textContent = status.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    while (detailStatus.firstChild) { detailStatus.removeChild(detailStatus.firstChild); }
    detailStatus.appendChild(badgeEl);

    // CUPS details (printer message from job data)
    if (jobData && jobData.printer_message) {
      detailCupsRow.style.display = '';
      detailCups.textContent = jobData.printer_message;
    } else {
      detailCupsRow.style.display = 'none';
    }

    // Coupon on failure
    if (status === 'failed' && jobData && jobData.coupon_code) {
      couponSection.style.display = '';
      couponCode.textContent = jobData.coupon_code;
    }

    // Terminal actions
    if (TERMINAL_STATES.indexOf(status) !== -1) {
      terminalActions.style.display = '';
      pollStatus.textContent = 'Final status: ' + status;
    }
  }

  // ── Polling ───────────────────────────────────────────────────────────────────

  var pollInterval = null;
  var pollCount    = 0;
  var MAX_POLLS    = 200; // Stop after ~10 minutes (200 × 3s)

  function startPolling() {
    applyUI(currentStatus, null);

    if (TERMINAL_STATES.indexOf(currentStatus) !== -1) {
      // Already terminal on page load — no need to poll
      terminalActions.style.display = '';
      pollStatus.textContent = 'Final.';
      return;
    }

    pollInterval = setInterval(poll, 3000);
  }

  function poll() {
    pollCount++;

    if (pollCount >= MAX_POLLS) {
      clearInterval(pollInterval);
      pollStatus.textContent = 'Stopped. Refresh to check again.';
      return;
    }

    pollStatus.textContent = 'Updated ' + new Date().toLocaleTimeString();

    fetch('/api/status/' + encodeURIComponent(jobId))
      .then(function (res) {
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        return res.json();
      })
      .then(function (jobData) {
        var newStatus = jobData.status;

        if (newStatus !== currentStatus) {
          currentStatus = newStatus;
          applyUI(newStatus, jobData);
        }

        // Stop polling on terminal state
        if (TERMINAL_STATES.indexOf(newStatus) !== -1) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
      })
      .catch(function (err) {
        // Network errors: keep polling silently, just log
        console.warn('success.js: poll error', err);
        pollStatus.textContent = 'Retrying…';
      });
  }

  // ── Boot ─────────────────────────────────────────────────────────────────────
  startPolling();

}());
