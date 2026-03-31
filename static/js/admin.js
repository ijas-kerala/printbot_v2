/**
 * static/js/admin.js — Admin dashboard controller.
 *
 * Responsibilities:
 *  - Tab switching (Jobs, Pricing, Export, Settings).
 *  - Printer status polling every 10s.
 *  - Revenue chart rendering via Chart.js.
 *  - Jobs table filtering + pagination.
 *  - Pricing rule CRUD (add, toggle active, delete).
 *  - Export CSV link builder.
 *  - Settings: save printer name, regenerate QR.
 *  - Coupon listing + manual coupon creation.
 *
 * No alert() — use showToast(). No innerHTML for user data.
 */

(function () {
  'use strict';

  // ── Read embedded data ────────────────────────────────────────────────────────
  var adminDataEl = document.getElementById('admin-data');
  if (!adminDataEl) { return; }
  var adminData;
  try { adminData = JSON.parse(adminDataEl.textContent); }
  catch (e) { console.error('admin.js: failed to parse admin data', e); return; }

  var revenueChartData = adminData.revenue_chart || [];
  var pricingRules     = adminData.pricing_rules  || [];

  // ── Tab switching ─────────────────────────────────────────────────────────────

  var tabButtons = document.querySelectorAll('.tab-item[data-tab]');
  var tabPanels  = {};

  tabButtons.forEach(function (btn) {
    var tabId = btn.dataset.tab;
    tabPanels[tabId] = document.getElementById('panel-' + tabId);

    btn.addEventListener('click', function () {
      tabButtons.forEach(function (b) {
        b.classList.remove('is-active');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('is-active');
      btn.setAttribute('aria-selected', 'true');

      Object.keys(tabPanels).forEach(function (id) {
        if (tabPanels[id]) {
          tabPanels[id].classList.toggle('is-active', id === tabId);
        }
      });

      // Lazy-init tab content
      if (tabId === 'settings') { loadCoupons(); }
    });
  });

  // ── Revenue chart ─────────────────────────────────────────────────────────────

  (function initChart() {
    var canvas = document.getElementById('revenue-chart');
    if (!canvas || typeof Chart === 'undefined') { return; }

    var labels  = revenueChartData.map(function (d) { return d.date; });
    var values  = revenueChartData.map(function (d) { return d.revenue; });

    new Chart(canvas, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: 'Revenue (₹)',
          data: values,
          backgroundColor: 'rgba(232,130,12,0.20)',
          borderColor:     'rgba(232,130,12,0.85)',
          borderWidth: 2,
          borderRadius: 4,
          hoverBackgroundColor: 'rgba(232,130,12,0.35)',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) { return '₹' + ctx.parsed.y.toFixed(2); },
            },
          },
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: {
              callback: function (v) { return '₹' + v; },
              font: { family: "'DM Sans', sans-serif", size: 11 },
              color: '#6B7280',
            },
            grid: { color: 'rgba(213,206,197,0.5)' },
          },
          x: {
            ticks: {
              font: { family: "'DM Sans', sans-serif", size: 11 },
              color: '#6B7280',
            },
            grid: { display: false },
          },
        },
      },
    });
  }());

  // ── Printer status polling ────────────────────────────────────────────────────

  var printerDot    = document.getElementById('printer-dot');
  var printerText   = document.getElementById('printer-status-text');
  var printerCheck  = document.getElementById('printer-last-check');

  function pollPrinterStatus() {
    fetch('/admin/api/printer-status')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        var online = data.is_online;
        var status = data.status_text || (online ? 'Online' : 'Offline');

        printerDot.className = 'printer-status-dot ' + (online ? 'online' : 'offline');
        printerText.textContent = status;
        printerCheck.textContent = 'Last checked: ' + new Date().toLocaleTimeString();
      })
      .catch(function () {
        printerDot.className = 'printer-status-dot unknown';
        printerText.textContent = 'Unable to reach printer daemon';
      });
  }

  pollPrinterStatus();
  setInterval(pollPrinterStatus, 10000);

  // ── Jobs table: filter + action buttons ──────────────────────────────────────

  var statusFilter = document.getElementById('job-status-filter');
  var jobSearch    = document.getElementById('job-search');
  var jobCountLbl  = document.getElementById('job-count-label');
  var jobsTbody    = document.getElementById('jobs-tbody');

  function filterJobs() {
    if (!jobsTbody) { return; }
    var statusVal = statusFilter ? statusFilter.value : '';
    var searchVal = jobSearch ? jobSearch.value.toLowerCase().trim() : '';

    var rows = jobsTbody.querySelectorAll('tr[data-job-id]');
    var visible = 0;

    rows.forEach(function (row) {
      var matchStatus = !statusVal || row.dataset.status === statusVal;
      var rowText = row.textContent.toLowerCase();
      var matchSearch = !searchVal || rowText.indexOf(searchVal) !== -1;
      var show = matchStatus && matchSearch;
      row.style.display = show ? '' : 'none';
      if (show) { visible++; }
    });

    if (jobCountLbl) {
      jobCountLbl.textContent = visible + ' job' + (visible !== 1 ? 's' : '');
    }
  }

  if (statusFilter) { statusFilter.addEventListener('change', filterJobs); }
  if (jobSearch)    { jobSearch.addEventListener('input', filterJobs); }
  filterJobs();

  // Retry / cancel job actions
  if (jobsTbody) {
    jobsTbody.addEventListener('click', function (e) {
      var retryBtn  = e.target.closest('.retry-btn');
      var cancelBtn = e.target.closest('.cancel-btn');

      if (retryBtn) {
        var jobId = retryBtn.dataset.jobId;
        retryBtn.disabled = true;
        retryBtn.textContent = '…';

        fetch('/admin/api/job/' + encodeURIComponent(jobId) + '/retry', { method: 'POST' })
          .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
          .then(function (r) {
            if (r.ok) {
              if (typeof showToast === 'function') { showToast('Job requeued.', 'success'); }
              setTimeout(function () { window.location.reload(); }, 800);
            } else {
              retryBtn.disabled = false;
              retryBtn.textContent = 'Retry';
              if (typeof showToast === 'function') {
                showToast(r.data.detail || 'Failed to retry job.', 'error');
              }
            }
          })
          .catch(function () {
            retryBtn.disabled = false;
            retryBtn.textContent = 'Retry';
            if (typeof showToast === 'function') { showToast('Network error.', 'error'); }
          });
      }

      if (cancelBtn) {
        var jobId2 = cancelBtn.dataset.jobId;
        if (!window.confirm('Cancel this job? This cannot be undone.')) { return; }
        cancelBtn.disabled = true;
        cancelBtn.textContent = '…';

        fetch('/admin/api/job/' + encodeURIComponent(jobId2) + '/cancel', { method: 'POST' })
          .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
          .then(function (r) {
            if (r.ok) {
              if (typeof showToast === 'function') { showToast('Job cancelled.', 'info'); }
              setTimeout(function () { window.location.reload(); }, 800);
            } else {
              cancelBtn.disabled = false;
              cancelBtn.textContent = 'Cancel';
              if (typeof showToast === 'function') {
                showToast(r.data.detail || 'Failed to cancel.', 'error');
              }
            }
          })
          .catch(function () {
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'Cancel';
          });
      }
    });
  }

  // ── Pricing CRUD ──────────────────────────────────────────────────────────────

  var addRuleBtn    = document.getElementById('add-rule-btn');
  var ruleFormError = document.getElementById('rule-form-error');
  var pricingTbody  = document.getElementById('pricing-tbody');

  function setPricingError(msg) {
    if (!ruleFormError) { return; }
    ruleFormError.textContent = msg;
    ruleFormError.style.display = msg ? '' : 'none';
  }

  if (addRuleBtn) {
    addRuleBtn.addEventListener('click', function () {
      setPricingError('');

      var minPages = parseInt(document.getElementById('rule-min').value, 10);
      var maxPagesRaw = document.getElementById('rule-max').value.trim();
      var maxPages = maxPagesRaw ? parseInt(maxPagesRaw, 10) : null;
      var isDuplex = document.getElementById('rule-type').value === 'duplex';
      var price    = parseFloat(document.getElementById('rule-price').value);
      var desc     = document.getElementById('rule-desc').value.trim();

      // Client-side validation
      if (isNaN(minPages) || minPages < 1) { setPricingError('Min pages must be ≥ 1.'); return; }
      if (maxPages !== null && maxPages <= minPages) { setPricingError('Max pages must be > min pages.'); return; }
      if (isNaN(price) || price <= 0) { setPricingError('Price per sheet must be > 0.'); return; }

      addRuleBtn.disabled = true;
      addRuleBtn.textContent = 'Adding…';

      fetch('/admin/api/pricing', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          min_pages:      minPages,
          max_pages:      maxPages,
          is_duplex:      isDuplex,
          price_per_page: price,
          description:    desc || null,
        }),
      })
        .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
        .then(function (r) {
          addRuleBtn.disabled = false;
          addRuleBtn.textContent = 'Add Rule';
          if (r.ok) {
            if (typeof showToast === 'function') { showToast('Pricing rule added.', 'success'); }
            setTimeout(function () { window.location.reload(); }, 500);
          } else {
            setPricingError(r.data.detail || 'Failed to add rule.');
          }
        })
        .catch(function () {
          addRuleBtn.disabled = false;
          addRuleBtn.textContent = 'Add Rule';
          setPricingError('Network error. Try again.');
        });
    });
  }

  // Toggle rule active / inactive
  if (pricingTbody) {
    pricingTbody.addEventListener('change', function (e) {
      var toggle = e.target.closest('.rule-active-toggle');
      if (!toggle) { return; }
      var ruleId  = toggle.dataset.ruleId;
      var active  = toggle.checked;

      fetch('/admin/api/pricing/' + ruleId + '/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: active }),
      })
        .then(function (res) {
          if (!res.ok) {
            toggle.checked = !active; // Revert
            if (typeof showToast === 'function') { showToast('Failed to update rule.', 'error'); }
          }
        })
        .catch(function () { toggle.checked = !active; });
    });

    // Delete rule
    pricingTbody.addEventListener('click', function (e) {
      var deleteBtn = e.target.closest('.delete-rule-btn');
      if (!deleteBtn) { return; }
      var ruleId = deleteBtn.dataset.ruleId;

      if (!window.confirm('Delete this pricing rule?')) { return; }

      fetch('/admin/api/pricing/' + ruleId, { method: 'DELETE' })
        .then(function (res) {
          if (res.ok) {
            var row = pricingTbody.querySelector('tr[data-rule-id="' + ruleId + '"]');
            if (row) { row.remove(); }
            if (typeof showToast === 'function') { showToast('Rule deleted.', 'info'); }
          } else {
            if (typeof showToast === 'function') { showToast('Failed to delete rule.', 'error'); }
          }
        })
        .catch(function () {
          if (typeof showToast === 'function') { showToast('Network error.', 'error'); }
        });
    });
  }

  // Test price calculator
  var testPriceBtn    = document.getElementById('test-price-btn');
  var testPriceResult = document.getElementById('test-price-result');

  if (testPriceBtn) {
    testPriceBtn.addEventListener('click', function () {
      var pages   = parseInt(document.getElementById('test-pages').value, 10);
      var isDuplex = document.querySelector('input[name="test-type"]:checked').value === 'duplex';

      if (isNaN(pages) || pages < 1) {
        if (testPriceResult) { testPriceResult.textContent = 'Enter a page count.'; }
        return;
      }

      fetch('/admin/api/pricing/test?pages=' + pages + '&duplex=' + isDuplex)
        .then(function (res) { return res.json(); })
        .then(function (d) {
          if (!testPriceResult) { return; }
          if (d.rule) {
            testPriceResult.textContent
              = pages + ' pages (' + (isDuplex ? 'duplex' : 'simplex') + ') → '
              + d.sheets + ' sheets × ₹' + d.price_per_sheet.toFixed(2)
              + ' = ₹' + d.total.toFixed(2)
              + ' — matched: "' + (d.rule.description || 'Rule #' + d.rule.id) + '"';
          } else {
            testPriceResult.textContent = 'No matching rule found for these parameters.';
          }
        })
        .catch(function () {
          if (testPriceResult) { testPriceResult.textContent = 'Error calculating price.'; }
        });
    });
  }

  // ── Export CSV ────────────────────────────────────────────────────────────────

  var exportMonthInput = document.getElementById('export-month');
  var exportBtn        = document.getElementById('export-btn');

  function updateExportLink() {
    if (!exportBtn || !exportMonthInput) { return; }
    var month = exportMonthInput.value; // YYYY-MM
    exportBtn.href = '/admin/export/csv?month=' + encodeURIComponent(month);
  }

  if (exportMonthInput) {
    exportMonthInput.addEventListener('change', updateExportLink);
    updateExportLink();
  }

  // ── Settings: save printer ────────────────────────────────────────────────────

  var savePrinterBtn   = document.getElementById('save-printer-btn');
  var printerNameInput = document.getElementById('printer-name-input');

  if (savePrinterBtn) {
    savePrinterBtn.addEventListener('click', function () {
      var name = printerNameInput ? printerNameInput.value.trim() : '';
      if (!name) {
        if (typeof showToast === 'function') { showToast('Enter a printer name.', 'warning'); }
        return;
      }
      savePrinterBtn.disabled = true;
      savePrinterBtn.textContent = '…';

      fetch('/admin/api/settings/printer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ printer_name: name }),
      })
        .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
        .then(function (r) {
          savePrinterBtn.disabled = false;
          savePrinterBtn.textContent = 'Save';
          if (r.ok) {
            if (typeof showToast === 'function') { showToast('Printer name saved.', 'success'); }
          } else {
            if (typeof showToast === 'function') { showToast(r.data.detail || 'Failed to save.', 'error'); }
          }
        })
        .catch(function () {
          savePrinterBtn.disabled = false;
          savePrinterBtn.textContent = 'Save';
          if (typeof showToast === 'function') { showToast('Network error.', 'error'); }
        });
    });
  }

  // ── Settings: regenerate QR ───────────────────────────────────────────────────

  var regenQrBtn = document.getElementById('regen-qr-btn');
  if (regenQrBtn) {
    regenQrBtn.addEventListener('click', function () {
      regenQrBtn.disabled = true;
      regenQrBtn.textContent = 'Regenerating…';

      fetch('/admin/api/regen-qr', { method: 'POST' })
        .then(function (res) { return res.json(); })
        .then(function () {
          regenQrBtn.disabled = false;
          regenQrBtn.textContent = 'Regenerate QR';
          // Bust cache on the QR image
          var qrImg = document.querySelector('img[src*="qr_code"]');
          if (qrImg) { qrImg.src = '/static/icons/qr_code.png?t=' + Date.now(); }
          if (typeof showToast === 'function') { showToast('QR code regenerated.', 'success'); }
        })
        .catch(function () {
          regenQrBtn.disabled = false;
          regenQrBtn.textContent = 'Regenerate QR';
          if (typeof showToast === 'function') { showToast('Failed to regenerate QR.', 'error'); }
        });
    });
  }

  // ── Coupons ───────────────────────────────────────────────────────────────────

  var couponsTbody = document.getElementById('coupons-tbody');

  function loadCoupons() {
    if (!couponsTbody) { return; }
    fetch('/admin/api/coupons')
      .then(function (res) { return res.json(); })
      .then(function (coupons) {
        while (couponsTbody.firstChild) { couponsTbody.removeChild(couponsTbody.firstChild); }

        if (coupons.length === 0) {
          var emptyRow = document.createElement('tr');
          var emptyCell = document.createElement('td');
          emptyCell.colSpan = 4;
          emptyCell.style.cssText = 'text-align:center; padding:1.5rem; color:var(--mist);';
          emptyCell.textContent = 'No coupons yet.';
          emptyRow.appendChild(emptyCell);
          couponsTbody.appendChild(emptyRow);
          return;
        }

        coupons.forEach(function (coupon) {
          var tr = document.createElement('tr');

          var tdCode = document.createElement('td');
          tdCode.style.fontFamily = "'Courier New', monospace";
          tdCode.style.fontWeight = '700';
          tdCode.style.letterSpacing = '0.06em';
          tdCode.textContent = coupon.code;   // textContent — safe

          var tdBalance = document.createElement('td');
          tdBalance.style.fontWeight = '600';
          tdBalance.textContent = '₹' + parseFloat(coupon.balance).toFixed(2);

          var tdCreated = document.createElement('td');
          tdCreated.style.cssText = 'font-size:0.8125rem; color:var(--mist);';
          tdCreated.textContent = coupon.created_at
            ? new Date(coupon.created_at).toLocaleDateString()
            : '—';

          var tdStatus = document.createElement('td');
          var badge = document.createElement('span');
          var isUsed = coupon.balance <= 0 || coupon.redeemed_at;
          badge.className = 'badge ' + (isUsed ? 'badge-expired' : 'badge-completed');
          badge.textContent = isUsed ? 'Redeemed' : 'Active';
          tdStatus.appendChild(badge);

          tr.appendChild(tdCode);
          tr.appendChild(tdBalance);
          tr.appendChild(tdCreated);
          tr.appendChild(tdStatus);
          couponsTbody.appendChild(tr);
        });
      })
      .catch(function () {
        var loadingEl = document.getElementById('coupons-loading');
        if (loadingEl) { loadingEl.textContent = 'Failed to load coupons.'; }
      });
  }

  // Create manual coupon
  var createCouponBtn    = document.getElementById('create-coupon-btn');
  var couponAmountInput  = document.getElementById('coupon-amount');
  var couponCreateResult = document.getElementById('coupon-create-result');

  if (createCouponBtn) {
    createCouponBtn.addEventListener('click', function () {
      var amount = parseFloat(couponAmountInput ? couponAmountInput.value : '');
      if (isNaN(amount) || amount <= 0) {
        if (typeof showToast === 'function') { showToast('Enter a valid amount.', 'warning'); }
        return;
      }
      createCouponBtn.disabled = true;
      createCouponBtn.textContent = 'Creating…';

      fetch('/admin/api/coupons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: amount }),
      })
        .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
        .then(function (r) {
          createCouponBtn.disabled = false;
          createCouponBtn.textContent = 'Create Coupon';

          if (r.ok && couponCreateResult) {
            couponCreateResult.textContent = 'Created: ' + r.data.code + ' (₹' + amount.toFixed(2) + ')';
            couponCreateResult.style.color = 'var(--forest)';
            if (couponAmountInput) { couponAmountInput.value = ''; }
            loadCoupons();
          } else {
            if (couponCreateResult) {
              couponCreateResult.textContent = (r.data && r.data.detail) ? r.data.detail : 'Failed to create.';
              couponCreateResult.style.color = 'var(--coral)';
            }
          }
        })
        .catch(function () {
          createCouponBtn.disabled = false;
          createCouponBtn.textContent = 'Create Coupon';
          if (typeof showToast === 'function') { showToast('Network error.', 'error'); }
        });
    });
  }

}());
