/**
 * static/js/payment.js — Razorpay checkout flow for PrintBot v3.
 *
 * Responsibilities:
 *  1. Read payment configuration from the #payment-data JSON blob.
 *  2. Initialise the Razorpay checkout modal.
 *  3. Auto-open the modal on page load.
 *  4. On Razorpay success callback: call POST /verify-payment with retry.
 *  5. On all retries exhausted: show fallback message + start polling
 *     /api/status/<job_id> until the webhook has processed the payment.
 *  6. On success: redirect to /success?job_id=<id>.
 *
 * Design rules (from .cursorrules):
 *  - No alert() — use showToast() from base.html or inline status messages.
 *  - No innerHTML for user content — use textContent.
 *  - No jQuery, no heavy frameworks.
 */

(function () {
  'use strict';

  // ── Read embedded configuration ────────────────────────────────────────────

  var dataEl = document.getElementById('payment-data');
  if (!dataEl) {
    console.error('payment.js: #payment-data element not found');
    return;
  }

  var cfg;
  try {
    cfg = JSON.parse(dataEl.textContent);
  } catch (e) {
    console.error('payment.js: failed to parse #payment-data JSON:', e);
    return;
  }

  // ── DOM references ─────────────────────────────────────────────────────────

  var payBtn    = document.getElementById('pay-btn');
  var statusEl  = document.getElementById('pay-status');

  function setStatus(msg, type) {
    // type: '' | 'is-error' | 'is-success'
    statusEl.textContent = msg;
    statusEl.className = 'pay-status' + (type ? ' ' + type : '');
  }

  function showSpinner(text) {
    // Replaces button text with a spinner + text (not innerHTML — builds DOM)
    payBtn.disabled = true;
    while (payBtn.firstChild) { payBtn.removeChild(payBtn.firstChild); }
    var spinner = document.createElement('span');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');
    var label = document.createElement('span');
    label.textContent = text || 'Processing…';
    payBtn.appendChild(spinner);
    payBtn.appendChild(label);
  }

  // ── Verify-payment with retry ───────────────────────────────────────────────

  /**
   * POST /verify-payment with exponential-backoff retry.
   *
   * @param {Object} response  Razorpay handler callback payload.
   * @param {number} maxRetries
   * @returns {Promise<{status: string, redirect: string} | null>}
   */
  async function verifyPayment(response, maxRetries) {
    maxRetries = maxRetries || 3;

    var body = JSON.stringify({
      razorpay_payment_id: response.razorpay_payment_id,
      razorpay_order_id:   response.razorpay_order_id,
      razorpay_signature:  response.razorpay_signature,
    });

    for (var attempt = 1; attempt <= maxRetries; attempt++) {
      if (attempt === 1) {
        setStatus('Payment received! Just a moment…');
      } else {
        setStatus('Still checking… (' + attempt + ' of ' + maxRetries + ')');
      }

      try {
        var res = await fetch('/verify-payment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
        });

        if (res.ok) {
          var data = await res.json();
          return data;
        }

        // Non-2xx: log and retry unless it's a 400 (bad signature — don't retry)
        var errText = 'HTTP ' + res.status;
        try { errText = (await res.json()).detail || errText; } catch (_) {}

        if (res.status === 400) {
          console.error('payment.js: server rejected signature:', errText);
          return null; // Hard failure — no point retrying
        }

        console.warn('payment.js: verify attempt', attempt, 'failed:', errText);

      } catch (networkErr) {
        console.warn('payment.js: verify attempt', attempt, 'network error:', networkErr);
      }

      // Exponential backoff between retries: 2s, 4s, 8s
      if (attempt < maxRetries) {
        await new Promise(function (resolve) {
          setTimeout(resolve, Math.pow(2, attempt) * 1000);
        });
      }
    }

    return null; // All retries exhausted
  }

  // ── Fallback polling ────────────────────────────────────────────────────────

  /**
   * Poll /jobs/<job_id>/status every 4 seconds until the job is in a terminal
   * state, then redirect.  Used when all verify-payment retries fail (e.g.
   * network hiccup) but the webhook may have already processed the payment.
   */
  function startFallbackPolling(jobId) {
    setStatus('Hang tight — confirming your payment…');

    var pollInterval = setInterval(async function () {
      try {
        var res = await fetch('/jobs/' + encodeURIComponent(jobId) + '/status');
        if (!res.ok) { return; } // Keep polling on temporary errors

        var data = await res.json();
        var s = data.status;

        if (s === 'paid' || s === 'processing' || s === 'printing' || s === 'completed') {
          clearInterval(pollInterval);
          setStatus('Payment confirmed!', 'is-success');
          if (typeof showToast === 'function') { showToast('Payment confirmed — preparing your print!', 'success'); }
          document.dispatchEvent(new CustomEvent('payment-success'));
          setTimeout(function () {
            window.location.href = '/success?job_id=' + encodeURIComponent(jobId);
          }, 600);
        }
        // Still payment_pending → keep polling silently
      } catch (_) {
        // Network error — keep polling
      }
    }, 4000);

    // Safety timeout: stop polling after 5 minutes and show a contact message
    setTimeout(function () {
      clearInterval(pollInterval);
      document.dispatchEvent(new CustomEvent('payment-failed'));
      setStatus(
        'We\'re still waiting for confirmation. Please show this order ID to staff: ' + cfg.order_id,
        'is-error'
      );
    }, 5 * 60 * 1000);
  }

  // ── Razorpay success handler ────────────────────────────────────────────────

  async function onPaymentSuccess(response) {
    // Hide the pay button immediately — prevents double-submission
    payBtn.style.display = 'none';

    var result = await verifyPayment(response, 3);

    if (result && result.redirect) {
      setStatus('All good! Taking you to your print status…', 'is-success');
      if (typeof showToast === 'function') { showToast('Payment successful — preparing your print!', 'success'); }
      document.dispatchEvent(new CustomEvent('payment-success'));
      setTimeout(function () {
        window.location.href = result.redirect;
      }, 400);
      return;
    }

    // All verify retries failed — check if the server rejected the signature
    if (result === null) {
      // Could be a bad signature (400) or network failure.
      // Try fallback polling — the webhook path may have already processed it.
      setStatus('Almost there — just confirming your payment…');
      startFallbackPolling(cfg.job_id);
    }
  }

  // ── Razorpay dismissal handler ──────────────────────────────────────────────

  function onModalDismiss() {
    // User closed the modal without paying — restore the button
    payBtn.style.display = '';
    payBtn.disabled = false;
    setStatus('No worries — payment cancelled. Tap Pay Now whenever you\'re ready.');
    document.dispatchEvent(new CustomEvent('payment-cancelled'));
  }

  // ── Initialise Razorpay ─────────────────────────────────────────────────────

  var options = {
    key:         cfg.key_id,
    amount:      cfg.amount_paise,   // in paise
    currency:    'INR',
    name:        cfg.shop_name || 'PrintBot',
    description: 'Print job payment',
    image:       '/static/icons/printo_idle.png',
    order_id:    cfg.order_id,

    handler: function (response) {
      showSpinner('Verifying…');
      onPaymentSuccess(response);
    },

    modal: {
      ondismiss: onModalDismiss,
      // Prevent accidental back-navigation while modal is open on mobile
      escape: true,
      backdropclose: false,
    },

    prefill: {
      // No real user data in a print kiosk — anonymous guest
      name:  'Guest',
      email: 'guest@printbot.local',
    },

    theme: {
      color: '#E8820C', // --saffron from design system
    },

    // Pass job_id as a note so it appears in the Razorpay dashboard
    notes: {
      job_id: cfg.job_id,
    },
  };

  // ── Mock payment handling ─────────────────────────────────────────────────
  if (cfg.is_mock) {
    payBtn.addEventListener('click', function (e) {
      e.preventDefault();
      showSpinner('Processing mock payment\u2026');
      setStatus('Simulating payment\u2026');
      setTimeout(function () {
        var mockResponse = {
          razorpay_payment_id: 'mock_pay_' + Date.now(),
          razorpay_order_id:   cfg.order_id,
          razorpay_signature:  'mock_signature',
        };
        onPaymentSuccess(mockResponse);
      }, 1200);
    });
    return;
  }

  // ── Initialise Razorpay ─────────────────────────────────────────────────────
  var rzp;
  try {
    rzp = new Razorpay(options);
  } catch (e) {
    console.error('payment.js: failed to initialise Razorpay:', e);
    setStatus('The payment page didn\'t load properly — try refreshing the page.', 'is-error');
    document.dispatchEvent(new CustomEvent('payment-failed'));
    return;
  }

  payBtn.addEventListener('click', function (e) {
    e.preventDefault();
    rzp.open();
  });

  window.addEventListener('load', function () {
    setTimeout(function () { rzp.open(); }, 350);
  });

}());
