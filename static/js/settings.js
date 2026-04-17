/**
 * static/js/settings.js — Print settings page controller (Prompt C rebuild).
 *
 * Responsibilities:
 *  1. Build the horizontal thumbnail strip from server-provided PNG URLs.
 *  2. Open / close the bottom-sheet panel for per-page include/exclude/rotation.
 *  3. Copies stepper (with tap-and-hold), duplex toggle, N-up layout selector.
 *  4. Page range input — parses "1-5, 8, 11-20" into selections.
 *  5. Coupon validation via POST /api/coupon/check (no full reload).
 *  6. recalcPrice() — pure JS math, updates price card and CTA, <50ms.
 *  7. Optimistic CTA: opens Razorpay modal immediately, confirms to server in parallel.
 *
 * State shape:
 *   files: [{ id: number, pages: [{ idx: number, include: boolean, rotation: number }] }]
 *
 * Design rules enforced:
 *   - textContent only for user content (never innerHTML for user data)
 *   - showToast() for global notifications, inline messages for field feedback
 *   - No alert(), no jQuery, no external libs
 *   - No server calls for price calculation
 */

(function () {
  'use strict';

  // ── Read embedded job data ──────────────────────────────────────────────────

  var dataEl = document.getElementById('job-data');
  if (!dataEl) { console.error('settings.js: #job-data not found'); return; }

  var jobData;
  try { jobData = JSON.parse(dataEl.textContent); }
  catch (e) { console.error('settings.js: failed to parse job-data', e); return; }

  var jobId          = jobData.job_id;
  var serverFiles    = jobData.files;
  var pricingRules   = jobData.pricing_rules || [];
  var priceFallback  = jobData.price_fallback || 2.0;
  var razorpayKeyId  = jobData.razorpay_key_id || '';
  var isMockPayment  = jobData.is_mock_payment || false;

  // ── State ────────────────────────────────────────────────────────────────────

  var state = {
    files: serverFiles.map(function (f) {
      var pages = [];
      for (var i = 0; i < f.page_count; i++) {
        pages.push({ idx: i, include: true, rotation: 0 });
      }
      return { id: f.id, pageCount: f.page_count, pages: pages };
    }),
    copies:           1,
    isDuplex:         false,
    nup:              1,
    coupon:           null,  // { code: string, discount: number } | null
    _sheetFileId:     null,
    _sheetPageIdx:    null,
    _sheetGlobalIdx:  null,
    _overlayWasOpen:  false,
    _filmstripBuilt:  false,
  };

  // Flat ordered list of all pages across all files — used by viewer navigation
  var _pageList = [];
  serverFiles.forEach(function (sf) {
    for (var i = 0; i < sf.page_count; i++) {
      _pageList.push({ fileId: sf.id, pageIdx: i });
    }
  });

  // ── DOM references ───────────────────────────────────────────────────────────

  var pagesGrid          = document.getElementById('pages-grid');
  var pagesOverlay       = document.getElementById('pages-overlay');
  var pagesOverlayBack   = document.getElementById('pages-overlay-backdrop');
  var pagesOverlayClose  = document.getElementById('pages-overlay-close');
  var pagesPreviewBtn    = document.getElementById('pages-preview-btn');
  var pagesToggleAllBtn  = document.getElementById('pages-toggle-all-btn');
  var pagesSummaryCount  = document.getElementById('pages-summary-count');
  var pagesOverlaySub    = document.getElementById('pages-overlay-sub');

  var noPagesMsg     = document.getElementById('no-pages-msg');
  var copiesValue    = document.getElementById('copies-value');
  var copiesInc      = document.getElementById('copies-inc');
  var copiesDec      = document.getElementById('copies-dec');
  var duplexToggle   = document.getElementById('duplex-toggle');
  var duplexHint     = document.getElementById('duplex-hint');
  var pricePages     = document.getElementById('price-pages');
  var priceSheets    = document.getElementById('price-sheets');
  var priceSheetsS   = document.getElementById('price-sheets-s');
  var pricePerSheet  = document.getElementById('price-per-sheet');
  var priceSubtotal  = document.getElementById('price-subtotal');
  var couponDiscRow  = document.getElementById('coupon-discount-row');
  var couponDiscVal  = document.getElementById('coupon-discount-value');
  var priceTotal     = document.getElementById('price-total');
  var ctaPrice       = document.getElementById('cta-price');
  var confirmBtn     = document.getElementById('confirm-btn');
  var moreToggle     = document.getElementById('more-options-toggle');
  var moreBody       = document.getElementById('more-options-body');
  var pageRangeInput = document.getElementById('page-range-input');
  var pageRangeError = document.getElementById('page-range-error');
  var couponInput    = document.getElementById('coupon-input');
  var couponApplyBtn = document.getElementById('coupon-apply-btn');
  var couponResult   = document.getElementById('coupon-result');
  var sheetBackdrop     = document.getElementById('sheet-backdrop');
  var bottomSheet       = document.getElementById('bottom-sheet');
  var sheetPreviewImg   = document.getElementById('sheet-preview-img');
  var sheetPreviewPaper = document.getElementById('sheet-preview-paper');
  var sheetPageCounter  = document.getElementById('sheet-page-counter');
  var sheetIncBtn       = document.getElementById('sheet-include-btn');
  var sheetExcBtn       = document.getElementById('sheet-exclude-btn');
  var sheetDoneBtn      = document.getElementById('sheet-done-btn');
  var sheetBackBtn      = document.getElementById('sheet-back-btn');
  var sheetPrevBtn      = document.getElementById('sheet-prev-btn');
  var sheetNextBtn      = document.getElementById('sheet-next-btn');
  var sheetFilmstrip    = document.getElementById('sheet-filmstrip');
  var orientBtns        = document.querySelectorAll('.orient-btn');
  var layoutBtns        = document.querySelectorAll('.layout-btn');

  // ── Pricing calculator ───────────────────────────────────────────────────────

  function totalSelectedPages() {
    var total = 0;
    state.files.forEach(function (f) {
      f.pages.forEach(function (p) { if (p.include) total++; });
    });
    return total;
  }

  function totalAllPages() {
    var total = 0;
    state.files.forEach(function (f) { total += f.pageCount; });
    return total;
  }

  function sheetsNeeded(pages, isDuplex, nup) {
    var pagesPerPhysicalSide = nup;
    var contentSheets = Math.ceil(pages / pagesPerPhysicalSide);
    return isDuplex ? Math.ceil(contentSheets / 2) : contentSheets;
  }

  function getPricePerSheet(sheetCount, isDuplex) {
    if (pricingRules && pricingRules.length > 0) {
      for (var i = 0; i < pricingRules.length; i++) {
        var r = pricingRules[i];
        if (r.is_active === false) continue;
        if (r.is_duplex !== isDuplex) continue;
        var inRange = sheetCount >= r.min_pages &&
                      (r.max_pages === null || sheetCount <= r.max_pages);
        if (inRange) return r.price_per_page;
      }
    }
    return priceFallback;
  }

  function recalcPrice() {
    var pages       = totalSelectedPages();
    var sheets      = sheetsNeeded(pages, state.isDuplex, state.nup);
    var totalSheets = sheets * state.copies;
    var unitPrice   = getPricePerSheet(totalSheets, state.isDuplex);
    var subtotal    = unitPrice * totalSheets;
    var discount    = state.coupon ? Math.min(state.coupon.discount, subtotal) : 0;
    var total       = Math.max(0, subtotal - discount);

    pricePages.textContent    = pages;
    priceSheets.textContent   = totalSheets;
    priceSheetsS.textContent  = totalSheets === 1 ? '' : 's';
    pricePerSheet.textContent = '\u20b9' + unitPrice.toFixed(2);
    priceSubtotal.textContent = '\u20b9' + subtotal.toFixed(2);
    priceTotal.textContent    = '\u20b9' + total.toFixed(2);
    ctaPrice.textContent      = '\u20b9' + total.toFixed(2);

    if (discount > 0) {
      couponDiscRow.style.display = '';
      couponDiscVal.textContent   = '\u2212\u20b9' + discount.toFixed(2);
    } else {
      couponDiscRow.style.display = 'none';
    }

    var canDuplex = pages >= 2;
    duplexToggle.disabled = !canDuplex;
    if (!canDuplex) {
      duplexHint.textContent = 'Select at least 2 pages to enable front & back printing';
      duplexHint.style.color = 'var(--warning)';
      if (state.isDuplex) {
        state.isDuplex = false;
        duplexToggle.checked = false;
      }
    } else {
      duplexHint.textContent = 'Saves paper — prints on both sides of each sheet';
      duplexHint.style.color = '';
    }

    // Summary bar + overlay header
    var allPages = totalAllPages();
    var skipped  = allPages - pages;
    var summaryText = skipped === 0
      ? 'All ' + allPages + ' page' + (allPages === 1 ? '' : 's') + ' selected'
      : pages + ' of ' + allPages + ' pages selected';
    if (pagesSummaryCount) pagesSummaryCount.textContent = summaryText;
    if (pagesOverlaySub) {
      pagesOverlaySub.textContent = skipped === 0
        ? 'Tap a page to preview it — use \u2713/\u2715 to skip'
        : skipped + ' page' + (skipped === 1 ? '' : 's') + ' skipped — tap \u2713/\u2715 chips to restore';
    }
    // "Restore all" only appears when something is skipped
    if (pagesToggleAllBtn) {
      pagesToggleAllBtn.style.display = skipped > 0 ? '' : 'none';
    }

    // CTA
    var canConfirm = pages > 0;
    confirmBtn.disabled = !canConfirm;
    noPagesMsg.style.display = canConfirm ? 'none' : '';
  }

  // ── Pages grid ───────────────────────────────────────────────────────────────

  function buildPagesGrid() {
    pagesGrid.innerHTML = '';

    serverFiles.forEach(function (serverFile) {
      var fileState = getFileState(serverFile.id);
      if (!fileState) return;

      var thumbUrls = serverFile.thumb_urls || [];
      for (var i = 0; i < serverFile.page_count; i++) {
        (function (pageIdx) {
          var pageState = fileState.pages[pageIdx];
          var item = document.createElement('div');
          item.className = 'thumb-item' + (pageState && pageState.include ? '' : ' is-excluded');
          item.dataset.fileId  = serverFile.id;
          item.dataset.pageIdx = pageIdx;
          item.setAttribute('role', 'button');
          item.setAttribute('tabindex', '0');
          item.setAttribute('aria-label', 'Page ' + (pageIdx + 1) + ' — tap to preview');

          // Thumbnail image — stored in data-src, loaded when overlay opens
          var thumbUrl = thumbUrls[pageIdx] || '';
          if (thumbUrl) {
            var skel = document.createElement('div');
            skel.className = 'thumb-skeleton skeleton';
            item.appendChild(skel);

            var img = document.createElement('img');
            img.className = 'thumb-canvas';
            img.alt       = 'Page ' + (pageIdx + 1);
            img.style.display = 'none';
            img.dataset.src   = thumbUrl;  // loaded on overlay open, not now

            img.onload = function () {
              skel.style.display = 'none';
              img.style.display  = 'block';
            };
            img.onerror = function () {
              skel.classList.remove('skeleton');
              skel.style.background = 'var(--cream)';
              skel.style.display = 'block';
            };
            item.appendChild(img);
          } else {
            var placeholder = document.createElement('div');
            placeholder.className = 'thumb-skeleton';
            placeholder.style.background = 'var(--cream)';
            item.appendChild(placeholder);
          }

          // Page number
          var label = document.createElement('span');
          label.className   = 'thumb-page-num';
          label.textContent = pageIdx + 1;
          item.appendChild(label);

          // Skip indicator — shown only on excluded pages, styled via CSS
          var badge = document.createElement('span');
          badge.className = 'thumb-state-badge';
          badge.setAttribute('aria-hidden', 'true');
          item.appendChild(badge);

          // Rotation badge — shown when page has non-zero rotation
          var rotBadge = document.createElement('span');
          rotBadge.className = 'thumb-rot-badge';
          rotBadge.setAttribute('aria-hidden', 'true');
          rotBadge.textContent = '\u21BB';  // ↻
          rotBadge.style.display = (pageState && pageState.rotation !== 0) ? 'flex' : 'none';
          item.appendChild(rotBadge);

          // Quick include/exclude chip (bottom-right corner)
          var chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'thumb-toggle-btn' + (pageState && pageState.include ? ' is-included' : ' is-excluded-chip');
          chip.setAttribute('aria-label', (pageState && pageState.include ? 'Skip' : 'Include') + ' page ' + (pageIdx + 1));
          chip.addEventListener('click', function (e) {
            e.stopPropagation();
            togglePageInclusion(serverFile.id, pageIdx);
          });
          item.appendChild(chip);

          // Tap item → open full-page viewer
          item.addEventListener('click', function (e) {
            if (e.target.closest('.thumb-toggle-btn')) return;
            openSheet(serverFile.id, pageIdx);
          });
          item.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              openSheet(serverFile.id, pageIdx);
            }
          });

          pagesGrid.appendChild(item);
        })(i);
      }
    });
  }

  function togglePageInclusion(fileId, pageIdx) {
    var pState = getPageState(fileId, pageIdx);
    if (!pState) return;
    pState.include = !pState.include;
    renderThumbnailStates();
    recalcPrice();
  }

  function renderThumbnailStates() {
    var items = pagesGrid.querySelectorAll('.thumb-item');
    items.forEach(function (item) {
      var fileId  = parseInt(item.dataset.fileId, 10);
      var pageIdx = parseInt(item.dataset.pageIdx, 10);
      var fState  = getFileState(fileId);
      if (!fState) return;
      var pState  = fState.pages[pageIdx];
      if (!pState) return;

      // Excluded overlay
      item.classList.toggle('is-excluded', !pState.include);

      // Quick-toggle chip
      var chip = item.querySelector('.thumb-toggle-btn');
      if (chip) {
        chip.classList.toggle('is-included', pState.include);
        chip.classList.toggle('is-excluded-chip', !pState.include);
        chip.setAttribute('aria-label', (pState.include ? 'Skip' : 'Include') + ' page ' + (pageIdx + 1));
      }

      // Rotation badge
      var rotBadge = item.querySelector('.thumb-rot-badge');
      if (rotBadge) {
        rotBadge.style.display = pState.rotation !== 0 ? 'flex' : 'none';
      }
    });

    // Mirror excluded state in filmstrip if it's built
    if (state._filmstripBuilt && sheetFilmstrip) {
      var filmItems = sheetFilmstrip.querySelectorAll('.filmstrip-item');
      filmItems.forEach(function (fi, idx) {
        var page = _pageList[idx];
        if (!page) return;
        var ps = getPageState(page.fileId, page.pageIdx);
        if (!ps) return;
        fi.classList.toggle('is-excluded', !ps.include);
      });
    }
  }

  // ── Full-page viewer (bottom-sheet) ─────────────────────────────────────────

  function openSheet(fileId, pageIdx) {
    var fState = getFileState(fileId);
    if (!fState) return;
    var pState = fState.pages[pageIdx];
    if (!pState) return;

    // Find global index in flat page list
    var globalIdx = -1;
    for (var gi = 0; gi < _pageList.length; gi++) {
      if (_pageList[gi].fileId === fileId && _pageList[gi].pageIdx === pageIdx) {
        globalIdx = gi;
        break;
      }
    }
    if (globalIdx === -1) return;

    // If the overlay is open, close it and remember to re-open on "Back"
    if (pagesOverlay.classList.contains('is-open')) {
      state._overlayWasOpen = true;
      closePagesOverlay();
    }

    state._sheetFileId    = fileId;
    state._sheetPageIdx   = pageIdx;
    state._sheetGlobalIdx = globalIdx;

    // Page counter
    sheetPageCounter.textContent = 'Page ' + (globalIdx + 1) + ' of ' + _pageList.length;

    // Preview image
    var serverFile = serverFiles.find(function (f) { return f.id === fileId; });
    var thumbUrl   = serverFile && serverFile.thumb_urls ? serverFile.thumb_urls[pageIdx] : '';
    sheetPreviewImg.src = thumbUrl || '';
    sheetPreviewImg.alt = 'Page ' + (pageIdx + 1);

    // Apply rotation/orientation
    updateSheetIncExc(pState.include);
    updateOrientToggle(pState.rotation);
    applyPreviewOrientation(pState.rotation);

    // Prev/next button disabled states
    sheetPrevBtn.disabled = globalIdx <= 0;
    sheetNextBtn.disabled = globalIdx >= _pageList.length - 1;

    // Build filmstrip once, then update active item
    if (!state._filmstripBuilt) {
      buildFilmstrip();
      state._filmstripBuilt = true;
    }
    updateFilmstripActive(globalIdx);

    // Open
    bottomSheet.classList.add('is-open');
    bottomSheet.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeSheet() {
    bottomSheet.classList.remove('is-open');
    bottomSheet.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    state._sheetFileId    = null;
    state._sheetPageIdx   = null;
    state._sheetGlobalIdx = null;
  }

  function navigateSheet(direction) {
    if (state._sheetGlobalIdx === null) return;
    var next = state._sheetGlobalIdx + direction;
    if (next < 0 || next >= _pageList.length) return;
    var page = _pageList[next];
    openSheet(page.fileId, page.pageIdx);
  }

  function buildFilmstrip() {
    sheetFilmstrip.innerHTML = '';
    _pageList.forEach(function (page, idx) {
      var sf       = serverFiles.find(function (f) { return f.id === page.fileId; });
      var thumbUrl = sf && sf.thumb_urls ? sf.thumb_urls[page.pageIdx] : '';
      var ps       = getPageState(page.fileId, page.pageIdx);

      var fi = document.createElement('button');
      fi.type = 'button';
      fi.className = 'filmstrip-item' + (ps && !ps.include ? ' is-excluded' : '');
      fi.dataset.globalIdx = idx;
      fi.setAttribute('aria-label', 'Jump to page ' + (idx + 1));

      if (thumbUrl) {
        var img = document.createElement('img');
        img.src = thumbUrl;
        img.alt = '';
        img.setAttribute('aria-hidden', 'true');
        fi.appendChild(img);
      } else {
        var ph = document.createElement('div');
        ph.className = 'filmstrip-placeholder';
        fi.appendChild(ph);
      }

      var num = document.createElement('span');
      num.textContent = idx + 1;
      fi.appendChild(num);

      (function (capturedIdx) {
        fi.addEventListener('click', function () {
          var p = _pageList[capturedIdx];
          openSheet(p.fileId, p.pageIdx);
        });
      })(idx);

      sheetFilmstrip.appendChild(fi);
    });
  }

  function updateFilmstripActive(globalIdx) {
    if (!sheetFilmstrip) return;
    var items = sheetFilmstrip.querySelectorAll('.filmstrip-item');
    items.forEach(function (fi, i) {
      fi.classList.toggle('is-active', i === globalIdx);
    });
    // Scroll active item into view
    var activeItem = sheetFilmstrip.querySelector('.filmstrip-item.is-active');
    if (activeItem) {
      activeItem.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    }
  }

  function updateSheetIncExc(isIncluded) {
    sheetIncBtn.classList.toggle('is-active', isIncluded);
    sheetExcBtn.classList.toggle('is-active', !isIncluded);
  }

  function updateOrientToggle(rotation) {
    var isLandscape = rotation === 90 || rotation === 270;
    orientBtns.forEach(function (btn) {
      var rot = parseInt(btn.dataset.rot, 10);
      var active = (rot === 0 && !isLandscape) || (rot === 90 && isLandscape);
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function applyPreviewOrientation(rotation) {
    var isLandscape = rotation === 90 || rotation === 270;
    // Switch paper frame between portrait and landscape aspect ratios
    sheetPreviewPaper.classList.toggle('is-landscape', isLandscape);
    // Apply CSS transform so users see the content rotated as it will print
    if (isLandscape) {
      // Scale down so the rotated portrait image fits inside the landscape frame
      sheetPreviewImg.style.transform = 'rotate(' + rotation + 'deg) scale(0.72)';
    } else if (rotation === 180) {
      sheetPreviewImg.style.transform = 'rotate(180deg)';
    } else {
      sheetPreviewImg.style.transform = '';
    }
  }

  sheetIncBtn.addEventListener('click', function () {
    if (state._sheetFileId === null) return;
    var pState = getPageState(state._sheetFileId, state._sheetPageIdx);
    if (!pState) return;
    pState.include = true;
    updateSheetIncExc(true);
    renderThumbnailStates();
    recalcPrice();
  });

  sheetExcBtn.addEventListener('click', function () {
    if (state._sheetFileId === null) return;
    var pState = getPageState(state._sheetFileId, state._sheetPageIdx);
    if (!pState) return;
    pState.include = false;
    updateSheetIncExc(false);
    renderThumbnailStates();
    recalcPrice();
  });

  orientBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (state._sheetFileId === null) return;
      var pState = getPageState(state._sheetFileId, state._sheetPageIdx);
      if (!pState) return;
      var deg = parseInt(btn.dataset.rot, 10);
      pState.rotation = deg;
      updateOrientToggle(deg);
      applyPreviewOrientation(deg);
      renderThumbnailStates();
    });
  });

  sheetPrevBtn.addEventListener('click', function () { navigateSheet(-1); });
  sheetNextBtn.addEventListener('click', function () { navigateSheet(1); });

  // "Back to grid" — close viewer and restore the overlay
  sheetBackBtn.addEventListener('click', function () {
    closeSheet();
    if (state._overlayWasOpen) {
      state._overlayWasOpen = false;
      openPagesOverlay();
    }
  });

  // "Done" — close viewer and overlay completely
  sheetDoneBtn.addEventListener('click', function () {
    closeSheet();
    state._overlayWasOpen = false;
  });

  // Swipe left/right to navigate between pages
  var sheetTouchStartX = 0;
  var sheetTouchStartY = 0;
  bottomSheet.addEventListener('touchstart', function (e) {
    sheetTouchStartX = e.touches[0].clientX;
    sheetTouchStartY = e.touches[0].clientY;
  }, { passive: true });
  bottomSheet.addEventListener('touchend', function (e) {
    var dx = e.changedTouches[0].clientX - sheetTouchStartX;
    var dy = e.changedTouches[0].clientY - sheetTouchStartY;
    // Only handle horizontal swipes (more horizontal than vertical)
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 50) {
      navigateSheet(dx < 0 ? 1 : -1);
    }
  }, { passive: true });

  // ── Pages overlay ────────────────────────────────────────────────────────────

  function openPagesOverlay() {
    pagesGrid.scrollTop = 0;
    pagesOverlay.classList.add('is-open');
    pagesOverlay.setAttribute('aria-hidden', 'false');
    pagesOverlayBack.classList.add('is-open');
    document.body.style.overflow = 'hidden';

    // Load all thumbnail images now — they were deferred until the overlay is visible
    pagesGrid.querySelectorAll('img[data-src]').forEach(function (img) {
      img.src = img.dataset.src;
      delete img.dataset.src;
    });
  }

  function closePagesOverlay() {
    pagesOverlay.classList.remove('is-open');
    pagesOverlay.setAttribute('aria-hidden', 'true');
    pagesOverlayBack.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  pagesPreviewBtn.addEventListener('click', openPagesOverlay);
  pagesOverlayClose.addEventListener('click', closePagesOverlay);
  pagesOverlayBack.addEventListener('click', closePagesOverlay);

  // ── All / None toggle ─────────────────────────────────────────────────────────

  if (pagesToggleAllBtn) {
    pagesToggleAllBtn.addEventListener('click', function () {
      // Always restores — button is only visible when pages are skipped
      state.files.forEach(function (f) {
        f.pages.forEach(function (p) { p.include = true; });
      });
      renderThumbnailStates();
      recalcPrice();
    });
  }

  // ── Copies stepper ───────────────────────────────────────────────────────────

  function updateCopiesUI() {
    copiesValue.textContent    = state.copies;
    copiesDec.disabled         = state.copies <= 1;
    copiesInc.disabled         = state.copies >= 99;
  }

  copiesDec.addEventListener('click', function () {
    if (state.copies > 1) { state.copies--; updateCopiesUI(); recalcPrice(); }
  });

  copiesInc.addEventListener('click', function () {
    if (state.copies < 99) { state.copies++; updateCopiesUI(); recalcPrice(); }
  });

  // Tap-and-hold on + to increment continuously
  var holdTimer = null;
  var holdInterval = null;

  function startHold() {
    holdTimer = setTimeout(function () {
      holdInterval = setInterval(function () {
        if (state.copies < 99) { state.copies++; updateCopiesUI(); recalcPrice(); }
        else { stopHold(); }
      }, 100);
    }, 500);
  }

  function stopHold() {
    clearTimeout(holdTimer);
    clearInterval(holdInterval);
    holdTimer = null;
    holdInterval = null;
  }

  copiesInc.addEventListener('mousedown',  startHold);
  copiesInc.addEventListener('touchstart', startHold, { passive: true });
  copiesInc.addEventListener('mouseup',    stopHold);
  copiesInc.addEventListener('mouseleave', stopHold);
  copiesInc.addEventListener('touchend',   stopHold);

  // ── Duplex toggle ────────────────────────────────────────────────────────────

  duplexToggle.addEventListener('change', function () {
    state.isDuplex = duplexToggle.checked;
    recalcPrice();
  });

  // ── N-up layout selector ─────────────────────────────────────────────────────

  layoutBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var nup = parseInt(btn.dataset.nup, 10);
      state.nup = nup;
      layoutBtns.forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
      recalcPrice();
    });
  });

  // ── More options toggle ──────────────────────────────────────────────────────

  moreToggle.addEventListener('click', function () {
    var isOpen = moreBody.classList.toggle('is-open');
    moreToggle.classList.toggle('is-open', isOpen);
    moreToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });

  // ── Page range input ─────────────────────────────────────────────────────────

  pageRangeInput.addEventListener('input', function () {
    var raw = pageRangeInput.value.trim();
    if (!raw) {
      // Reset: include all pages
      pageRangeError.style.display = 'none';
      pageRangeInput.classList.remove('is-error');
      state.files.forEach(function (f) {
        f.pages.forEach(function (p) { p.include = true; });
      });
      renderThumbnailStates();
      recalcPrice();
      return;
    }

    // Parse range across all files in order
    var totalPages = 0;
    state.files.forEach(function (f) { totalPages += f.pageCount; });

    var parsed = parsePageRange(raw, totalPages);
    if (parsed === null) {
      pageRangeError.style.display = '';
      pageRangeInput.classList.add('is-error');
      return;
    }

    pageRangeError.style.display = 'none';
    pageRangeInput.classList.remove('is-error');

    // Apply across files sequentially
    var globalIdx = 0;
    state.files.forEach(function (f) {
      f.pages.forEach(function (p) {
        // parsed is a Set of 0-based global indices
        p.include = parsed.has(globalIdx);
        globalIdx++;
      });
    });

    renderThumbnailStates();
    recalcPrice();
  });

  /**
   * Parse a page range string like "1-5, 8, 11-20" into a Set of 0-based indices.
   * Returns null if the syntax is invalid.
   * Pages are 1-indexed in the UI; we convert to 0-based internally.
   */
  function parsePageRange(str, maxPages) {
    var result = new Set();
    var parts  = str.split(',');
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (!part) continue;
      var rangeMatch = part.match(/^(\d+)\s*-\s*(\d+)$/);
      var singleMatch = part.match(/^(\d+)$/);
      if (rangeMatch) {
        var from = parseInt(rangeMatch[1], 10) - 1;
        var to   = parseInt(rangeMatch[2], 10) - 1;
        if (from < 0 || to < from || to >= maxPages) return null;
        for (var j = from; j <= to; j++) result.add(j);
      } else if (singleMatch) {
        var n = parseInt(singleMatch[1], 10) - 1;
        if (n < 0 || n >= maxPages) return null;
        result.add(n);
      } else {
        return null;
      }
    }
    if (result.size === 0) return null;
    return result;
  }

  // ── Coupon ───────────────────────────────────────────────────────────────────

  couponInput.addEventListener('input', function () {
    couponInput.value = couponInput.value.toUpperCase();
    // Clear result when user edits
    couponResult.style.display = 'none';
    // If a coupon was applied, remove it when the code changes
    if (state.coupon && couponInput.value !== state.coupon.code) {
      state.coupon = null;
      recalcPrice();
    }
  });

  couponApplyBtn.addEventListener('click', function () {
    var code = couponInput.value.trim();
    if (!code) return;

    couponResult.style.display = '';
    couponResult.style.color   = 'var(--muted)';
    couponResult.textContent   = 'Checking\u2026';
    couponApplyBtn.disabled    = true;

    fetch('/api/coupon/check', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ code: code, job_id: jobId }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.valid) {
          state.coupon = { code: code, discount: data.discount };
          couponResult.style.color = 'var(--success)';
          couponResult.textContent = data.message;
          recalcPrice();
          // Notify mascot of the happy event
          document.dispatchEvent(new CustomEvent('coupon-applied'));
        } else {
          state.coupon = null;
          couponResult.style.color = 'var(--error)';
          couponResult.textContent = data.message || 'Hmm, that code isn\'t valid. Double-check it and try again!';
          recalcPrice();
        }
      })
      .catch(function () {
        couponResult.style.color = 'var(--error)';
        couponResult.textContent = 'Hmm, we couldn\'t check that code. Try again in a moment.';
      })
      .finally(function () {
        couponApplyBtn.disabled = false;
      });
  });

  // ── Confirm & Pay ────────────────────────────────────────────────────────────

  confirmBtn.addEventListener('click', function () {
    if (confirmBtn.disabled) return;

    confirmBtn.classList.add('btn-loading');
    confirmBtn.disabled = true;

    doConfirmRequest(null, function (data) {
      if (data.status === 'free') {
        window.location.href = data.redirect;
        return;
      }

      if (data.order_id) {
        window.location.href = '/payment?order_id=' + encodeURIComponent(data.order_id);
      } else {
        confirmBtn.classList.remove('btn-loading');
        confirmBtn.disabled = false;
        if (typeof showToast === 'function') showToast('Oops! Something hiccuped before payment — tap Confirm & Pay again.', 'error');
      }
    });
  });

  function doConfirmRequest(modal, onSuccess) {
    var body = {
      job_id:      jobId,
      copies:      state.copies,
      is_duplex:   state.isDuplex,
      nup_layout:  state.nup,
      coupon_code: state.coupon ? state.coupon.code : null,
      files:       state.files.map(function (f) {
        return {
          id:    f.id,
          pages: f.pages.map(function (p) {
            return { idx: p.idx, include: p.include, rotation: p.rotation };
          }),
        };
      }),
    };

    fetch('/api/settings/confirm', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (err) {
            throw new Error(err.detail || 'Confirm failed.');
          });
        }
        return res.json();
      })
      .then(function (data) {
        onSuccess(data);
      })
      .catch(function (err) {
        confirmBtn.classList.remove('btn-loading');
        confirmBtn.disabled = false;
        var detail = err && err.message ? ' (' + err.message + ')' : '';
        var msg = 'Something hiccuped! Please tap Confirm & Pay again.' + detail;
        if (typeof showToast === 'function') showToast(msg, 'error');
      });
  }

  // ── State helpers ────────────────────────────────────────────────────────────

  function getFileState(fileId) {
    return state.files.find(function (f) { return f.id === fileId; }) || null;
  }

  function getPageState(fileId, pageIdx) {
    var f = getFileState(fileId);
    return f ? f.pages[pageIdx] || null : null;
  }

  // ── Boot ─────────────────────────────────────────────────────────────────────

  buildPagesGrid();
  updateCopiesUI();
  recalcPrice();

})();
