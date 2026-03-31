/**
 * static/js/settings.js — Print settings page controller.
 *
 * Responsibilities:
 *  1. Render PDF.js thumbnails lazily via IntersectionObserver.
 *  2. Manage per-page include/exclude and rotation state.
 *  3. Listen for N-up / copies / duplex changes and recalculate price.
 *  4. Parse page range input ("1-5, 8, 10-12") and update selection.
 *  5. Handle coupon application via AJAX.
 *  6. On "Confirm & Pay": POST to /api/settings/confirm, open Razorpay.
 *
 * State shape:
 *   files: [{
 *     id: number,
 *     pageCount: number,
 *     pages: [{ idx: number, include: boolean, rotation: number }]
 *   }]
 *
 * Design rules:
 *  - textContent only for user-visible content.
 *  - showToast() for global notifications.
 *  - No alert(), no jQuery.
 */

(function () {
  'use strict';

  // ── Read embedded job data ────────────────────────────────────────────────────
  var dataEl = document.getElementById('job-data');
  if (!dataEl) { console.error('settings.js: #job-data not found'); return; }

  var jobData;
  try { jobData = JSON.parse(dataEl.textContent); }
  catch (e) { console.error('settings.js: failed to parse job data', e); return; }

  var jobId        = jobData.job_id;
  var serverFiles  = jobData.files;       // [{id, original_name, page_count, ...}]
  var pricingRules = jobData.pricing_rules; // [{min_pages, max_pages, is_duplex, price_per_page}]
  var priceFallback = jobData.price_fallback;

  // ── State ─────────────────────────────────────────────────────────────────────

  var state = {
    files: serverFiles.map(function (f) {
      var pages = [];
      for (var i = 0; i < f.page_count; i++) {
        pages.push({ idx: i, include: true, rotation: 0 });
      }
      return { id: f.id, pageCount: f.page_count, pages: pages };
    }),
    copies:   1,
    isDuplex: false,
    nup:      1,
    coupon:   null,   // { code, discount } or null
  };

  // ── DOM references ────────────────────────────────────────────────────────────
  var copiesValue    = document.getElementById('copies-value');
  var copiesInc      = document.getElementById('copies-inc');
  var copiesDec      = document.getElementById('copies-dec');
  var duplexToggle   = document.getElementById('duplex-toggle');
  var duplexHint     = document.getElementById('duplex-hint');
  var confirmBtn     = document.getElementById('confirm-btn');
  var confirmStatus  = document.getElementById('confirm-status');
  var noPagesMsg     = document.getElementById('no-pages-msg');
  var couponInput    = document.getElementById('coupon-input');
  var couponApplyBtn = document.getElementById('coupon-apply-btn');
  var couponApplied  = document.getElementById('coupon-applied-msg');
  var couponAppliedT = document.getElementById('coupon-applied-text');
  var couponRemoveBtn= document.getElementById('coupon-remove-btn');
  var couponErrorMsg = document.getElementById('coupon-error-msg');
  var couponInputRow = document.getElementById('coupon-input-row');

  // Price display
  var pricePages    = document.getElementById('price-pages');
  var priceSheets   = document.getElementById('price-sheets');
  var priceCopies   = document.getElementById('price-copies');
  var pricePerSheet = document.getElementById('price-per-sheet');
  var priceTotal    = document.getElementById('price-total');
  var couponDiscRow = document.getElementById('coupon-discount-row');
  var couponDiscVal = document.getElementById('coupon-discount-value');

  // ── Pricing calculator ────────────────────────────────────────────────────────

  function totalSelectedPages() {
    var total = 0;
    state.files.forEach(function (f) {
      f.pages.forEach(function (p) { if (p.include) { total++; } });
    });
    return total;
  }

  function sheetsNeeded(pages, isDuplex, nup) {
    // Pages after N-up grouping
    var pagesPerSheet = nup;
    var sheetsForContent = Math.ceil(pages / pagesPerSheet);
    // Duplex halves the sheets (rounded up)
    if (isDuplex) {
      return Math.ceil(sheetsForContent / 2);
    }
    return sheetsForContent;
  }

  /**
   * Find the matching pricing rule for a given (page count, isDuplex) pair.
   * Returns price_per_page or the fallback.
   */
  function getPricePerSheet(sheetCount, isDuplex) {
    var matched = null;
    if (pricingRules && pricingRules.length > 0) {
      for (var i = 0; i < pricingRules.length; i++) {
        var rule = pricingRules[i];
        if (rule.is_active === false) { continue; }
        if (rule.is_duplex !== isDuplex) { continue; }
        var inRange = sheetCount >= rule.min_pages
          && (rule.max_pages === null || sheetCount <= rule.max_pages);
        if (inRange) { matched = rule; break; }
      }
    }
    return matched ? matched.price_per_page : priceFallback;
  }

  function recalcPrice() {
    var pages  = totalSelectedPages();
    var sheets = sheetsNeeded(pages, state.isDuplex, state.nup);
    var totalSheets = sheets * state.copies;
    var unitPrice   = getPricePerSheet(totalSheets, state.isDuplex);
    var subtotal    = unitPrice * totalSheets;
    var discount    = 0;
    if (state.coupon) {
      discount = Math.min(state.coupon.discount, subtotal);
    }
    var total = Math.max(0, subtotal - discount);

    // Update price display
    pricePages.textContent    = pages;
    priceSheets.textContent   = totalSheets;
    priceCopies.textContent   = state.copies;
    pricePerSheet.textContent = '₹' + unitPrice.toFixed(2);
    priceTotal.textContent    = '₹' + total.toFixed(2);

    if (discount > 0) {
      couponDiscRow.style.display = '';
      couponDiscVal.textContent   = '−₹' + discount.toFixed(2);
    } else {
      couponDiscRow.style.display = 'none';
    }

    // Duplex needs ≥ 2 pages
    var canDuplex = pages >= 2;
    duplexToggle.disabled = !canDuplex;
    duplexHint.style.display = !canDuplex ? '' : 'none';
    if (!canDuplex && state.isDuplex) {
      state.isDuplex = false;
      duplexToggle.checked = false;
    }

    // Confirm button state
    var canConfirm = pages > 0;
    confirmBtn.disabled = !canConfirm;
    confirmBtn.setAttribute('aria-disabled', canConfirm ? 'false' : 'true');
    noPagesMsg.style.display = canConfirm ? 'none' : '';

    return { pages, sheets: totalSheets, unitPrice, subtotal, discount, total };
  }

  // ── Thumbnails via PDF.js ────────────────────────────────────────────────────

  var renderQueues  = {};   // fileId → queue of pageIdx to render
  var activeRenders = {};   // fileId → boolean

  /**
   * Create skeleton placeholder elements for all pages of a file.
   * Real canvases are swapped in when the grid comes into view.
   */
  function initSkeletons(fileId, pageCount) {
    var grid = document.getElementById('thumb-grid-' + fileId);
    if (!grid) { return; }

    var fState = state.files.find(function (f) { return f.id === fileId; });

    for (var i = 0; i < pageCount; i++) {
      (function (idx) {
        var wrapper = document.createElement('div');
        wrapper.dataset.pageIdx = idx;
        wrapper.className = 'thumb-item' + (fState && fState.pages[idx].include ? ' is-selected' : ' is-excluded');
        wrapper.setAttribute('aria-label', 'Page ' + (idx + 1));

        var skeleton = document.createElement('div');
        skeleton.className = 'thumb-skeleton';
        wrapper.appendChild(skeleton);

        var pageNum = document.createElement('div');
        pageNum.className = 'thumb-page-num';
        pageNum.textContent = idx + 1;
        wrapper.appendChild(pageNum);

        var controls = buildThumbControls(fileId, idx);
        wrapper.appendChild(controls);

        grid.appendChild(wrapper);
      })(i);
    }
  }

  function buildThumbControls(fileId, pageIdx) {
    var fState = state.files.find(function (f) { return f.id === fileId; });
    var pState = fState ? fState.pages[pageIdx] : { include: true, rotation: 0 };

    var row = document.createElement('div');
    row.className = 'thumb-controls';

    // Rotation button
    var rotBtn = document.createElement('button');
    rotBtn.type = 'button';
    rotBtn.className = 'thumb-rotate-btn';
    rotBtn.setAttribute('aria-label', 'Rotate page ' + (pageIdx + 1));
    rotBtn.title = 'Rotate 90°';
    rotBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
    rotBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      rotatePage(fileId, pageIdx);
    });

    // Include/exclude button
    var inclBtn = document.createElement('button');
    inclBtn.type = 'button';
    inclBtn.className = 'thumb-include-btn ' + (pState.include ? 'is-included' : 'is-excluded');
    inclBtn.setAttribute('aria-label', (pState.include ? 'Exclude' : 'Include') + ' page ' + (pageIdx + 1));
    inclBtn.innerHTML = pState.include
      ? '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>'
      : '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    inclBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      togglePage(fileId, pageIdx);
    });

    row.appendChild(rotBtn);
    row.appendChild(inclBtn);
    return row;
  }

  /**
   * Start rendering thumbnails for a file's grid using PDF.js.
   * Uses IntersectionObserver for lazy loading.
   */
  function attachObserver(fileId, pageCount) {
    var grid = document.getElementById('thumb-grid-' + fileId);
    if (!grid) { return; }
    if (typeof IntersectionObserver === 'undefined') {
      // FALLBACK: render all immediately if IO not supported
      renderAllThumbnails(fileId, pageCount);
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          observer.unobserve(entry.target);
          var idx = parseInt(entry.target.dataset.pageIdx, 10);
          queueRender(fileId, idx, entry.target);
        }
      });
    }, { rootMargin: '200px 0px' });

    var wrappers = grid.querySelectorAll('.thumb-item');
    wrappers.forEach(function (w) { observer.observe(w); });
  }

  function queueRender(fileId, pageIdx, wrapper) {
    if (!renderQueues[fileId]) { renderQueues[fileId] = []; }
    renderQueues[fileId].push({ pageIdx: pageIdx, wrapper: wrapper });
    if (!activeRenders[fileId]) { processQueue(fileId); }
  }

  function processQueue(fileId) {
    var q = renderQueues[fileId];
    if (!q || q.length === 0) { activeRenders[fileId] = false; return; }
    activeRenders[fileId] = true;

    var item = q.shift();
    renderThumb(fileId, item.pageIdx, item.wrapper).then(function () {
      processQueue(fileId);
    });
  }

  /**
   * Render a single page as a canvas thumbnail using PDF.js.
   * Falls back to a labelled box if rendering fails.
   */
  function renderThumb(fileId, pageIdx, wrapper) {
    var grid = document.getElementById('thumb-grid-' + fileId);
    if (!grid) { return Promise.resolve(); }

    var fileUrl = grid.dataset.fileUrl + pageIdx;

    if (typeof pdfjsLib === 'undefined') {
      // PDF.js not loaded — show page number only
      replaceSkeleton(wrapper, null, pageIdx);
      return Promise.resolve();
    }

    return pdfjsLib.getDocument(fileUrl).promise
      .then(function (pdf) {
        return pdf.getPage(1); // Each thumbnail endpoint returns single-page doc
      })
      .then(function (page) {
        var viewport = page.getViewport({ scale: 1 });
        var targetWidth = 100;
        var scale = targetWidth / viewport.width;
        var scaledViewport = page.getViewport({ scale: scale });

        var canvas = document.createElement('canvas');
        canvas.className = 'thumb-canvas';
        canvas.width  = scaledViewport.width;
        canvas.height = scaledViewport.height;

        var ctx = canvas.getContext('2d');
        return page.render({ canvasContext: ctx, viewport: scaledViewport }).promise
          .then(function () { return canvas; });
      })
      .then(function (canvas) {
        replaceSkeleton(wrapper, canvas, pageIdx);
      })
      .catch(function (err) {
        // EDGE CASE: thumbnail not yet available (DOCX still converting)
        // Show page number box as fallback
        replaceSkeleton(wrapper, null, pageIdx);
      });
  }

  function replaceSkeleton(wrapper, canvas, pageIdx) {
    var skeleton = wrapper.querySelector('.thumb-skeleton');
    if (!skeleton) { return; }

    if (canvas) {
      wrapper.insertBefore(canvas, skeleton);
    } else {
      // FALLBACK: render a labelled placeholder
      var ph = document.createElement('div');
      ph.style.cssText = 'padding-bottom:141%; background:var(--cream); display:flex; align-items:center; justify-content:center; font-size:0.75rem; color:var(--mist); position:relative;';
      var label = document.createElement('span');
      label.style.cssText = 'position:absolute; top:50%; left:50%; transform:translate(-50%,-50%)';
      label.textContent = 'p.' + (pageIdx + 1);
      ph.appendChild(label);
      wrapper.insertBefore(ph, skeleton);
    }

    wrapper.removeChild(skeleton);
  }

  function renderAllThumbnails(fileId, pageCount) {
    var grid = document.getElementById('thumb-grid-' + fileId);
    if (!grid) { return; }
    var wrappers = grid.querySelectorAll('.thumb-item');
    wrappers.forEach(function (w) {
      var idx = parseInt(w.dataset.pageIdx, 10);
      queueRender(fileId, idx, w);
    });
  }

  // ── Per-page controls ────────────────────────────────────────────────────────

  function getThumbWrapper(fileId, pageIdx) {
    var grid = document.getElementById('thumb-grid-' + fileId);
    if (!grid) { return null; }
    return grid.querySelector('[data-page-idx="' + pageIdx + '"]');
  }

  function refreshThumbState(fileId, pageIdx) {
    var wrapper = getThumbWrapper(fileId, pageIdx);
    if (!wrapper) { return; }

    var fState = state.files.find(function (f) { return f.id === fileId; });
    var pState = fState ? fState.pages[pageIdx] : null;
    if (!pState) { return; }

    wrapper.classList.toggle('is-selected', pState.include);
    wrapper.classList.toggle('is-excluded', !pState.include);

    var canvas = wrapper.querySelector('canvas');
    if (canvas && pState.rotation !== undefined) {
      canvas.style.transform = 'rotate(' + pState.rotation + 'deg)';
      // Swap width/height for 90/270 to avoid clipping
      var is90or270 = (pState.rotation === 90 || pState.rotation === 270);
      canvas.style.transformOrigin = 'center center';
    }

    // Rebuild controls
    var existingControls = wrapper.querySelector('.thumb-controls');
    if (existingControls) { wrapper.removeChild(existingControls); }
    wrapper.appendChild(buildThumbControls(fileId, pageIdx));
  }

  function togglePage(fileId, pageIdx) {
    var fState = state.files.find(function (f) { return f.id === fileId; });
    if (!fState) { return; }
    fState.pages[pageIdx].include = !fState.pages[pageIdx].include;
    refreshThumbState(fileId, pageIdx);
    recalcPrice();
  }

  function rotatePage(fileId, pageIdx) {
    var fState = state.files.find(function (f) { return f.id === fileId; });
    if (!fState) { return; }
    fState.pages[pageIdx].rotation = (fState.pages[pageIdx].rotation + 90) % 360;
    refreshThumbState(fileId, pageIdx);
  }

  // ── Select all / Deselect all ────────────────────────────────────────────────

  function selectAllForFile(fileId, include) {
    var fState = state.files.find(function (f) { return f.id === fileId; });
    if (!fState) { return; }
    fState.pages.forEach(function (p) { p.include = include; });

    var grid = document.getElementById('thumb-grid-' + fileId);
    if (grid) {
      var wrappers = grid.querySelectorAll('.thumb-item');
      wrappers.forEach(function (w) {
        var idx = parseInt(w.dataset.pageIdx, 10);
        refreshThumbState(fileId, idx);
      });
    }
    recalcPrice();
  }

  document.querySelectorAll('.select-all-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      selectAllForFile(parseInt(btn.dataset.fileId, 10), true);
    });
  });
  document.querySelectorAll('.deselect-all-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      selectAllForFile(parseInt(btn.dataset.fileId, 10), false);
    });
  });

  // ── Page range parser ────────────────────────────────────────────────────────

  /**
   * Parse "1-5, 8, 10-12" into a Set of 0-based page indices.
   * Returns null if the string is empty (no-op).
   * Returns Set or throws for invalid syntax.
   */
  function parsePageRange(rangeStr, maxPage) {
    rangeStr = rangeStr.trim();
    if (!rangeStr) { return null; }

    var result = new Set();
    var parts  = rangeStr.split(',');

    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (!part) { continue; }

      var dashIdx = part.indexOf('-');
      if (dashIdx > 0) {
        var from = parseInt(part.substring(0, dashIdx).trim(), 10);
        var to   = parseInt(part.substring(dashIdx + 1).trim(), 10);
        if (isNaN(from) || isNaN(to) || from < 1 || to > maxPage || from > to) {
          throw new Error('Invalid range: ' + part);
        }
        for (var p = from; p <= to; p++) { result.add(p - 1); }
      } else {
        var pg = parseInt(part, 10);
        if (isNaN(pg) || pg < 1 || pg > maxPage) {
          throw new Error('Invalid page: ' + part);
        }
        result.add(pg - 1);
      }
    }
    return result;
  }

  function applyPageRange(fileId, rangeStr) {
    var fState = state.files.find(function (f) { return f.id === fileId; });
    if (!fState) { return; }

    var set;
    try { set = parsePageRange(rangeStr, fState.pageCount); }
    catch (e) {
      if (typeof showToast === 'function') {
        showToast(e.message, 'error');
      }
      return;
    }

    if (set === null) {
      // Empty — no change
      return;
    }

    fState.pages.forEach(function (p) {
      p.include = set.has(p.idx);
    });

    var grid = document.getElementById('thumb-grid-' + fileId);
    if (grid) {
      var wrappers = grid.querySelectorAll('.thumb-item');
      wrappers.forEach(function (w) {
        refreshThumbState(fileId, parseInt(w.dataset.pageIdx, 10));
      });
    }
    recalcPrice();
  }

  document.querySelectorAll('.apply-range-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var fileId = parseInt(btn.dataset.fileId, 10);
      var input  = document.querySelector('.page-range-input[data-file-id="' + fileId + '"]');
      if (input) { applyPageRange(fileId, input.value); }
    });
  });
  document.querySelectorAll('.page-range-input').forEach(function (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        applyPageRange(parseInt(input.dataset.fileId, 10), input.value);
      }
    });
  });

  // ── File section collapse/expand ─────────────────────────────────────────────

  document.querySelectorAll('.file-section-head').forEach(function (head) {
    head.addEventListener('click', function () {
      var section = head.closest('.file-section');
      var isOpen  = section.classList.contains('is-open');
      // Close all
      document.querySelectorAll('.file-section').forEach(function (s) {
        s.classList.remove('is-open');
        s.querySelector('.file-section-head').setAttribute('aria-expanded', 'false');
      });
      // Toggle clicked
      if (!isOpen) {
        section.classList.add('is-open');
        head.setAttribute('aria-expanded', 'true');
      }
    });
    head.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); head.click(); }
    });
  });

  // ── Copies stepper ───────────────────────────────────────────────────────────

  copiesInc.addEventListener('click', function () {
    if (state.copies < 99) {
      state.copies++;
      copiesValue.textContent = state.copies;
      copiesDec.disabled = false;
      if (state.copies >= 99) { copiesInc.disabled = true; }
      recalcPrice();
    }
  });
  copiesDec.addEventListener('click', function () {
    if (state.copies > 1) {
      state.copies--;
      copiesValue.textContent = state.copies;
      copiesInc.disabled = false;
      if (state.copies <= 1) { copiesDec.disabled = true; }
      recalcPrice();
    }
  });
  copiesDec.disabled = true; // Starts at 1

  // ── Duplex toggle ────────────────────────────────────────────────────────────

  duplexToggle.addEventListener('change', function () {
    state.isDuplex = duplexToggle.checked;
    recalcPrice();
  });

  // ── N-up radio group ──────────────────────────────────────────────────────────

  document.querySelectorAll('input[name="nup"]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      state.nup = parseInt(radio.value, 10);
      recalcPrice();
    });
  });

  // ── Coupon ───────────────────────────────────────────────────────────────────

  function setCouponError(msg) {
    couponErrorMsg.textContent = msg;
    couponErrorMsg.style.display = msg ? '' : 'none';
  }

  couponApplyBtn.addEventListener('click', function () {
    var code = couponInput.value.trim().toUpperCase();
    if (!code) { setCouponError('Enter a coupon code.'); return; }
    setCouponError('');
    couponApplyBtn.disabled = true;

    var origText = couponApplyBtn.textContent;
    couponApplyBtn.textContent = '…';

    fetch('/api/coupon/check?code=' + encodeURIComponent(code))
      .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
      .then(function (r) {
        couponApplyBtn.disabled = false;
        couponApplyBtn.textContent = origText;

        if (!r.ok || !r.data.valid) {
          setCouponError(r.data.message || 'Invalid coupon code.');
          return;
        }

        state.coupon = { code: code, discount: r.data.balance };
        couponInputRow.style.display    = 'none';
        couponApplied.style.display     = '';
        couponAppliedT.textContent      = 'Coupon "' + code + '" applied (−₹' + r.data.balance.toFixed(2) + ')';
        recalcPrice();
        if (typeof showToast === 'function') { showToast('Coupon applied!', 'success'); }
      })
      .catch(function () {
        couponApplyBtn.disabled = false;
        couponApplyBtn.textContent = origText;
        setCouponError('Could not verify coupon. Try again.');
      });
  });

  couponRemoveBtn.addEventListener('click', function () {
    state.coupon = null;
    couponInput.value = '';
    couponApplied.style.display   = 'none';
    couponInputRow.style.display  = '';
    setCouponError('');
    recalcPrice();
  });

  // Allow pressing Enter in coupon input
  couponInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { couponApplyBtn.click(); }
  });

  // ── Confirm & Pay ────────────────────────────────────────────────────────────

  confirmBtn.addEventListener('click', function () {
    if (confirmBtn.disabled) { return; }

    confirmBtn.disabled = true;
    while (confirmBtn.firstChild) { confirmBtn.removeChild(confirmBtn.firstChild); }
    var spinner = document.createElement('span');
    spinner.className = 'spinner spinner-sm';
    spinner.setAttribute('aria-hidden', 'true');
    var label = document.createElement('span');
    label.textContent = 'Creating order…';
    confirmBtn.appendChild(spinner);
    confirmBtn.appendChild(label);
    confirmStatus.textContent = '';

    var payload = {
      job_id:     jobId,
      files:      state.files.map(function (f) {
        return {
          file_item_id: f.id,
          page_configs: f.pages.map(function (p) {
            return { page_idx: p.idx, rotation: p.rotation, include: p.include };
          }),
        };
      }),
      copies:     state.copies,
      is_duplex:  state.isDuplex,
      nup_layout: state.nup,
      coupon_code: state.coupon ? state.coupon.code : null,
    };

    fetch('/api/settings/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        return res.json().then(function (d) { return { ok: res.ok, data: d }; });
      })
      .then(function (r) {
        if (!r.ok) {
          resetConfirmBtn();
          var msg = r.data.detail || 'Failed to create order. Please try again.';
          confirmStatus.textContent = msg;
          if (typeof showToast === 'function') { showToast(msg, 'error'); }
          return;
        }

        var d = r.data;

        // EDGE CASE: coupon fully covers cost — skip Razorpay
        if (d.amount_paise === 0) {
          confirmStatus.textContent = 'Free! Redirecting…';
          setTimeout(function () {
            window.location.href = '/success?job_id=' + encodeURIComponent(jobId);
          }, 500);
          return;
        }

        // Open Razorpay modal
        openRazorpay(d);
      })
      .catch(function (err) {
        resetConfirmBtn();
        confirmStatus.textContent = 'Network error. Please try again.';
        console.error('settings.js: confirm error', err);
      });
  });

  function resetConfirmBtn() {
    confirmBtn.disabled = totalSelectedPages() === 0;
    while (confirmBtn.firstChild) { confirmBtn.removeChild(confirmBtn.firstChild); }
    var svg = document.createElement('span');
    svg.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>';
    var text = document.createElement('span');
    text.textContent = 'Confirm & Pay';
    confirmBtn.appendChild(svg.firstChild);
    confirmBtn.appendChild(text);
  }

  function openRazorpay(orderData) {
    if (typeof Razorpay === 'undefined') {
      // Razorpay JS not loaded — navigate to payment page as fallback
      window.location.href = '/payment?job_id=' + encodeURIComponent(jobId);
      return;
    }

    var options = {
      key:         orderData.key_id,
      amount:      orderData.amount_paise,
      currency:    'INR',
      name:        orderData.shop_name || 'PrintBot',
      description: 'Print job',
      order_id:    orderData.order_id,
      image:       '/static/icons/printo_idle.png',
      theme:       { color: '#E8820C' },
      notes:       { job_id: jobId },
      prefill:     { name: 'Guest', email: 'guest@printbot.local' },

      handler: function (response) {
        confirmStatus.textContent = 'Payment received! Verifying…';

        fetch('/verify-payment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            razorpay_payment_id: response.razorpay_payment_id,
            razorpay_order_id:   response.razorpay_order_id,
            razorpay_signature:  response.razorpay_signature,
          }),
        })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (data.redirect) {
              confirmStatus.textContent = 'Redirecting…';
              window.location.href = data.redirect;
            } else {
              window.location.href = '/success?job_id=' + encodeURIComponent(jobId);
            }
          })
          .catch(function () {
            // FALLBACK: navigate to success page regardless — webhook will catch it
            window.location.href = '/success?job_id=' + encodeURIComponent(jobId);
          });
      },

      modal: {
        ondismiss: function () {
          resetConfirmBtn();
          confirmStatus.textContent = 'Payment cancelled.';
        },
      },
    };

    try {
      var rzp = new Razorpay(options);
      rzp.open();
    } catch (e) {
      console.error('settings.js: Razorpay init failed', e);
      window.location.href = '/payment?job_id=' + encodeURIComponent(jobId);
    }
  }

  // ── Boot ─────────────────────────────────────────────────────────────────────

  // Load Razorpay checkout script dynamically (not needed until confirm)
  function loadRazorpay() {
    if (typeof Razorpay !== 'undefined') { return; }
    var s = document.createElement('script');
    s.src = 'https://checkout.razorpay.com/v1/checkout.js';
    document.head.appendChild(s);
  }

  // Initialise skeletons and observers for each file
  state.files.forEach(function (fState) {
    initSkeletons(fState.id, fState.pageCount);
    attachObserver(fState.id, fState.pageCount);
  });

  recalcPrice();
  loadRazorpay();

}());
