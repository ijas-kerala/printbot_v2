/**
 * static/js/success.js
 *
 * Drives the post-payment status page. Polls /jobs/<job_id>/status every 2 s
 * and updates the DOM to reflect current job state — no full page reloads.
 *
 * States handled:
 *   paid / processing         → working mascot, "Getting your document ready"
 *   printing (confirmed)      → working mascot, queue info + time anchor
 *   printing (estimated)      → waiting mascot, prominent job ID pill
 *   completed                 → happy mascot + CSS burst animation, "All done!"
 *   failed (with coupon)      → sad mascot, coupon card revealed
 *   failed / expired          → sad mascot, "Show your Job ID to staff"
 *
 * Timeout: after MAX_POLLS (150 × 2 s = 5 min) with no terminal state,
 * the page transitions to ESTIMATED regardless of status_source from server.
 */
(function () {
  'use strict';

  // ── Constants ────────────────────────────────────────────────────────────────

  const POLL_INTERVAL_MS  = 2000;
  const POLL_INTERVAL_ERR = 4000;  // Silent back-off on network / server errors
  const MAX_POLLS         = 150;   // ~5 minutes before client-side timeout
  const TERMINAL_STATUSES = new Set(['completed', 'failed', 'expired']);

  // ── Bootstrap ────────────────────────────────────────────────────────────────

  const successData = JSON.parse(
    document.getElementById('success-data').textContent
  );
  const JOB_ID = successData.job_id;

  let pollCount  = 0;
  let pollTimer  = null;
  let intervalMs = POLL_INTERVAL_MS;
  let lastStatus = successData.initial_status;
  let burstFired = false;

  // ── DOM references ───────────────────────────────────────────────────────────

  const mascotEl       = document.querySelector('.dot-mascot');
  const stepLabelEl    = document.getElementById('step-label');
  const headlineEl     = document.getElementById('status-headline');
  const descEl         = document.getElementById('status-desc');
  const jobIdPillEl    = document.getElementById('job-id-pill');
  const queueInfoEl    = document.getElementById('queue-info');
  const timeAnchorEl   = document.getElementById('time-anchor');
  const couponCardEl   = document.getElementById('coupon-card');
  const couponCodeEl   = document.getElementById('coupon-code-value');
  const printAnotherEl = document.getElementById('print-another-wrap');
  const burstEl        = document.getElementById('burst-container');
  const rippleEl       = document.getElementById('ripple-container');
  const copyJobIdBtn   = document.getElementById('copy-job-id');
  const copyCouponBtn  = document.getElementById('copy-coupon');

  // ── Utility ───────────────────────────────────────────────────────────────────

  // setMascotState(el, state) is provided by mascot.js (loaded before this script)

  function stopPolling() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function showTerminal() {
    printAnotherEl.hidden = false;
    // step-label element removed — flow-steps indicator already shows Done at step 4
    if (stepLabelEl) stepLabelEl.textContent = 'Done';
  }

  function triggerBurst() {
    if (burstFired) return;
    burstFired = true;
    burstEl.classList.add('burst-active');
  }

  function triggerRipple() {
    if (!rippleEl) return;
    // Reset to allow re-trigger on refresh
    rippleEl.classList.remove('ripple-active');
    void rippleEl.offsetWidth;
    rippleEl.classList.add('ripple-active');
  }

  /**
   * Play a short ascending chime using the Web Audio API (no audio file needed).
   * Three notes: E5 → G5 → C6 — a bright, positive major-triad arpeggio.
   * Respects user gesture requirements; fails silently if audio is unavailable.
   */
  function playCompletionChime() {
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;

      var ctx = new AC();

      // notes: [frequency_hz, start_offset_s, duration_s, peak_gain]
      var notes = [
        [659.25, 0.00, 0.20, 0.30],   // E5
        [783.99, 0.13, 0.20, 0.28],   // G5
        [1046.5, 0.26, 0.35, 0.22],   // C6
      ];

      notes.forEach(function (n) {
        var freq = n[0], offset = n[1], dur = n[2], peak = n[3];
        var osc  = ctx.createOscillator();
        var gain = ctx.createGain();

        osc.connect(gain);
        gain.connect(ctx.destination);

        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, 0);

        var t = ctx.currentTime + offset;
        gain.gain.setValueAtTime(0, t);
        gain.gain.linearRampToValueAtTime(peak, t + 0.010);         // 10 ms attack
        gain.gain.exponentialRampToValueAtTime(0.001, t + dur);     // smooth decay

        osc.start(t);
        osc.stop(t + dur + 0.05);
      });

      // Close context once all notes have finished to free resources
      setTimeout(function () {
        try { ctx.close(); } catch (e2) {}
      }, (0.26 + 0.35 + 0.1) * 1000);
    } catch (e) {
      // Audio unavailable — fail silently
    }
  }

  /** Convert seconds to a human-readable string, e.g. "about 4 minutes". */
  function formatWait(seconds) {
    if (!seconds || seconds <= 0) return null;
    var minutes = Math.round(seconds / 60);
    return minutes <= 1 ? 'about 1 minute' : 'about ' + minutes + ' minutes';
  }

  /**
   * Write text to clipboard and briefly replace the button content with "Copied",
   * restoring the original SVG icon after 1.5 s.
   */
  function copyText(text, btn) {
    if (!navigator.clipboard) return;
    var savedHTML = btn.innerHTML;
    navigator.clipboard.writeText(text).then(function () {
      btn.textContent = 'Copied';
      btn.style.cssText = 'font-size:0.75rem;font-family:var(--font-body);color:var(--success);';
      setTimeout(function () {
        btn.innerHTML = savedHTML;
        btn.style.cssText = '';
      }, 1500);
    }).catch(function () {});
  }

  // ── Copy button handlers ──────────────────────────────────────────────────────

  if (copyJobIdBtn) {
    copyJobIdBtn.addEventListener('click', function () {
      copyText(JOB_ID, copyJobIdBtn);
    });
  }

  if (copyCouponBtn) {
    copyCouponBtn.addEventListener('click', function () {
      var code = couponCodeEl.textContent;
      if (code) copyText(code, copyCouponBtn);
    });
  }

  // ── State renderers ───────────────────────────────────────────────────────────

  function renderProcessing() {
    setMascotState(mascotEl, 'working');
    headlineEl.textContent = 'Getting your document ready';
    descEl.textContent     = 'Almost there.';
    jobIdPillEl.classList.remove('prominent');
    queueInfoEl.hidden  = true;
    timeAnchorEl.hidden = true;
    couponCardEl.hidden = true;
  }

  function renderPrintingConfirmed(queueAhead, estimatedWait) {
    setMascotState(mascotEl, 'working');
    headlineEl.textContent = 'Printing now';
    descEl.textContent     = 'Collect your pages from the output tray when done.';
    jobIdPillEl.classList.remove('prominent');
    couponCardEl.hidden = true;

    // Only update queue info text when queue_ahead changes — avoids layout jank
    if (queueAhead > 0) {
      var label   = queueAhead === 1 ? '1 job ahead of you' : queueAhead + ' jobs ahead of you';
      var waitStr = formatWait(estimatedWait);
      queueInfoEl.textContent = waitStr ? label + ' \u2014 ' + waitStr : label;
      queueInfoEl.hidden = false;
    } else {
      queueInfoEl.hidden = true;
    }

    timeAnchorEl.hidden = false;
  }

  function renderPrintingEstimated() {
    setMascotState(mascotEl, 'waiting');
    headlineEl.textContent = 'Sent to the printer';
    descEl.textContent     = 'It should be printing now. If nothing appears in a few minutes, show your Job ID to staff.';
    // PRINTER_ESTIMATE: make job ID visually prominent so the user can show staff
    jobIdPillEl.classList.add('prominent');
    queueInfoEl.hidden  = true;
    timeAnchorEl.hidden = true;
    couponCardEl.hidden = true;
  }

  function renderCompleted() {
    // Multi-bounce celebration state
    mascotEl.classList.remove('dot-celebrating');
    void mascotEl.offsetWidth; // reflow to restart animation
    setMascotState(mascotEl, 'happy');
    mascotEl.classList.add('dot-celebrating');

    headlineEl.textContent = 'All done!';
    // Pop animation on headline
    headlineEl.classList.remove('headline-celebrating');
    void headlineEl.offsetWidth;
    headlineEl.classList.add('headline-celebrating');

    descEl.textContent     = 'Your pages are ready. Collect them from the output tray below the printer.';
    jobIdPillEl.classList.remove('prominent');
    queueInfoEl.hidden  = true;
    timeAnchorEl.hidden = true;
    couponCardEl.hidden = true;

    triggerBurst();
    triggerRipple();

    // Completion chime — slight delay so it lands with the burst/ripple
    setTimeout(playCompletionChime, 60);

    // Confetti shower — slight delay so it coincides with burst peak
    if (typeof launchConfetti === 'function') {
      setTimeout(function () { launchConfetti(mascotEl); }, 120);
    }

    stopPolling();
    showTerminal();
  }

  function renderFailed(couponCode) {
    setMascotState(mascotEl, 'sad');
    jobIdPillEl.classList.remove('prominent');
    queueInfoEl.hidden  = true;
    timeAnchorEl.hidden = true;

    if (couponCode) {
      headlineEl.textContent   = 'Something went wrong';
      descEl.textContent       = 'Your payment has been credited as a reprint code.';
      couponCodeEl.textContent = couponCode;
      couponCardEl.hidden      = false;
    } else {
      headlineEl.textContent = 'Print could not complete';
      descEl.textContent     = 'Please show your Job ID to nearby staff.';
      couponCardEl.hidden    = true;
    }

    stopPolling();
    showTerminal();
  }

  // ── Main update dispatcher ────────────────────────────────────────────────────

  function applyUpdate(jobData) {
    var status       = jobData.status;
    var statusSource = jobData.status_source || 'confirmed';
    var queueAhead   = jobData.queue_ahead   || 0;
    var estimatedWait = jobData.estimated_wait || 0;
    var couponCode   = jobData.coupon_code    || null;

    lastStatus = status;

    if (status === 'processing' || status === 'paid') {
      renderProcessing();

    } else if (status === 'printing') {
      // PRINTER_ESTIMATE: "estimated" from server or forced by client-side timeout
      if (statusSource === 'estimated') {
        renderPrintingEstimated();
      } else {
        renderPrintingConfirmed(queueAhead, estimatedWait);
      }

    } else if (status === 'completed') {
      renderCompleted();

    } else if (status === 'failed' || status === 'expired') {
      renderFailed(couponCode);

    }
    // Unknown statuses: leave UI unchanged — defensive against future status values
  }

  // ── Polling loop ──────────────────────────────────────────────────────────────

  function poll() {
    if (pollCount >= MAX_POLLS) {
      /*
       * PRINTER_ESTIMATE: client-side timeout — the job never reached a terminal
       * state in 5 minutes.  Show ESTIMATED rather than FAILED; the printer may
       * still be printing (CUPS doesn't guarantee real-time completion events).
       */
      if (!TERMINAL_STATUSES.has(lastStatus)) {
        renderPrintingEstimated();
        stopPolling();
        showTerminal();
      }
      return;
    }

    fetch('/jobs/' + JOB_ID + '/status', {
      headers:     { 'Accept': 'application/json' },
      credentials: 'same-origin',
    })
      .then(function (res) {
        if (!res.ok) {
          // Server error (5xx) — back off silently, no UI change
          intervalMs = POLL_INTERVAL_ERR;
          return null;
        }
        return res.json();
      })
      .then(function (jobData) {
        if (!jobData) {
          // Error branch — already handled above
          pollCount++;
          pollTimer = setTimeout(poll, intervalMs);
          return;
        }

        intervalMs = POLL_INTERVAL_MS;
        applyUpdate(jobData);

        if (TERMINAL_STATUSES.has(jobData.status)) {
          return;  // Poll stopped inside applyUpdate (renderCompleted/renderFailed)
        }

        pollCount++;
        pollTimer = setTimeout(poll, intervalMs);
      })
      .catch(function () {
        // Network error — back off silently, no UI change
        intervalMs = POLL_INTERVAL_ERR;
        pollCount++;
        pollTimer = setTimeout(poll, intervalMs);
      });
  }

  // ── Initialise ────────────────────────────────────────────────────────────────

  (function init() {
    var initialStatus = successData.initial_status;

    // Page loaded after job already reached a terminal state (e.g. user refreshed)
    if (TERMINAL_STATUSES.has(initialStatus)) {
      applyUpdate({ status: initialStatus, status_source: 'confirmed' });
      return;
    }

    // Apply an initial render so the page shows the correct state immediately
    applyUpdate({
      status:       initialStatus,
      status_source: 'confirmed',
      queue_ahead:  0,
    });

    // Begin polling
    pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
  }());

}());
