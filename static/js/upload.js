/**
 * static/js/upload.js — Multi-file drag-and-drop upload handler.
 *
 * Responsibilities:
 *  - Manage a local file list (add / remove entries)
 *  - Validate: accepted types, 90 MB total, max 20 files
 *  - Render file list rows with type icon, name, size, remove button, PDF page estimate
 *  - Show live total-size counter with color thresholds (warn >70 MB, danger >85 MB)
 *  - Enable/disable the Submit button based on state
 *  - Upload via XHR for progress tracking (not fetch — no upload progress events)
 *  - Phantom progress: 8% → 15% before real data flows
 *  - Show inline error messages without alert()
 *
 * No external dependencies. Uses textContent (never innerHTML) for user data.
 */

(function () {
  'use strict';

  // ── Constants ────────────────────────────────────────────────────────────────
  var MAX_TOTAL_BYTES  = 90 * 1024 * 1024;   // 90 MB
  var WARN_BYTES       = 70 * 1024 * 1024;   // 70 MB — yellow
  var DANGER_BYTES     = 85 * 1024 * 1024;   // 85 MB — red
  var MAX_FILES        = 20;
  // Read first 200 KB of PDF to estimate page count (covers most catalogs)
  var PDF_SCAN_BYTES   = 200 * 1024;

  var ACCEPTED_MIME = [
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'image/jpeg',
    'image/png',
  ];
  var ACCEPTED_EXT = ['pdf', 'docx', 'doc', 'jpg', 'jpeg', 'png'];

  // ── DOM references ───────────────────────────────────────────────────────────
  var form              = document.getElementById('upload-form');
  var dropZone          = document.getElementById('drop-zone');
  var browseTrigger     = document.getElementById('dz-browse-trigger');
  var fileInput         = document.getElementById('file-input');
  var fileListSection   = document.getElementById('file-list-section');
  var fileListEl        = document.getElementById('file-list');
  var clearAllBtn       = document.getElementById('clear-all-btn');
  var submitBtn         = document.getElementById('submit-btn');
  var sizeBar           = document.getElementById('size-bar');
  var sizeCounterLabel  = document.getElementById('size-counter-label');
  var sizeOverMsg       = document.getElementById('size-over-msg');
  var fileCountNote     = document.getElementById('file-count-note');
  var uploadErrorEl     = document.getElementById('upload-error');
  var progressWrap      = document.getElementById('upload-progress-wrap');
  var progressBar       = document.getElementById('upload-progress');

  // ── Internal state ───────────────────────────────────────────────────────────
  // Each entry: { id: number, file: File }
  var fileList = [];
  var nextId   = 1;

  // ── Helpers ──────────────────────────────────────────────────────────────────

  function formatBytes(bytes) {
    if (bytes < 1024)          { return bytes + ' B'; }
    if (bytes < 1024 * 1024)   { return (bytes / 1024).toFixed(1) + ' KB'; }
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function getExt(filename) {
    var parts = filename.split('.');
    return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
  }

  function getFileType(file) {
    var ext = getExt(file.name);
    if (ext === 'pdf') { return 'pdf'; }
    if (ext === 'docx' || ext === 'doc') { return 'docx'; }
    if (ext === 'jpg' || ext === 'jpeg' || ext === 'png') { return 'img'; }
    // Fallback: check MIME type
    if (file.type === 'application/pdf') { return 'pdf'; }
    if (file.type.startsWith('image/')) { return 'img'; }
    return 'unknown';
  }

  function isAccepted(file) {
    var ext = getExt(file.name);
    if (ACCEPTED_EXT.indexOf(ext) !== -1) { return true; }
    if (ACCEPTED_MIME.indexOf(file.type) !== -1) { return true; }
    return false;
  }

  function totalBytes() {
    return fileList.reduce(function (sum, entry) { return sum + entry.file.size; }, 0);
  }

  function showError(msg) {
    uploadErrorEl.style.display = '';
    while (uploadErrorEl.firstChild) { uploadErrorEl.removeChild(uploadErrorEl.firstChild); }
    var alertEl = document.createElement('div');
    alertEl.className = 'alert alert-error';
    var icon = document.createElement('svg');
    icon.setAttribute('class', 'alert-icon');
    icon.setAttribute('width', '16');
    icon.setAttribute('height', '16');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '2');
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    icon.setAttribute('aria-hidden', 'true');
    // SECURITY: static SVG markup only
    icon.innerHTML = '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>';
    var text = document.createElement('span');
    text.textContent = msg;
    alertEl.appendChild(icon);
    alertEl.appendChild(text);
    uploadErrorEl.appendChild(alertEl);

    setTimeout(function () { uploadErrorEl.style.display = 'none'; }, 5000);
  }

  function clearError() {
    uploadErrorEl.style.display = 'none';
  }

  // ── UI rendering ─────────────────────────────────────────────────────────────

  function updateSizeBar() {
    var used   = totalBytes();
    var pct    = Math.min((used / MAX_TOTAL_BYTES) * 100, 100);
    var isOver = used > MAX_TOTAL_BYTES;

    sizeBar.style.width = pct + '%';
    sizeBar.classList.toggle('is-over', isOver);
    sizeBar.classList.toggle('is-full', pct >= 99.9 && !isOver);

    sizeCounterLabel.textContent = formatBytes(used) + ' / 90 MB';

    // Color thresholds: warning at 70 MB, danger at 85 MB, error at limit
    if (isOver || used > DANGER_BYTES) {
      sizeCounterLabel.style.color = 'var(--error)';
    } else if (used > WARN_BYTES) {
      sizeCounterLabel.style.color = 'var(--warning)';
    } else {
      sizeCounterLabel.style.color = '';
    }

    sizeOverMsg.style.display = isOver ? '' : 'none';

    var n = fileList.length;
    fileCountNote.textContent = n === 1 ? '1 file' : n + ' files';
  }

  function updateSubmitState() {
    var valid = fileList.length > 0 && totalBytes() <= MAX_TOTAL_BYTES;
    submitBtn.disabled = !valid;
    submitBtn.setAttribute('aria-disabled', valid ? 'false' : 'true');
  }

  function setSubmitLoading() {
    submitBtn.disabled = true;
    while (submitBtn.firstChild) { submitBtn.removeChild(submitBtn.firstChild); }
    var label = document.createElement('span');
    label.textContent = 'Uploading\u2026';
    submitBtn.appendChild(label);
  }

  function resetSubmitButton() {
    submitBtn.disabled = false;
    submitBtn.setAttribute('aria-disabled', 'false');
    while (submitBtn.firstChild) { submitBtn.removeChild(submitBtn.firstChild); }
    // Restore original button contents: chevron icon + label
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '18');
    svg.setAttribute('height', '18');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2.2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    // SECURITY: static SVG
    svg.innerHTML = '<polyline points="9 18 15 12 9 6"/>';
    var label = document.createElement('span');
    label.textContent = 'Continue to Print Settings';
    submitBtn.appendChild(svg);
    submitBtn.appendChild(label);
  }

  /**
   * Build a single file-item row element.
   * Uses textContent / setAttribute — never innerHTML for user data.
   */
  function buildFileRow(entry) {
    var type = getFileType(entry.file);

    var row = document.createElement('div');
    row.className = 'file-item';
    row.setAttribute('role', 'listitem');
    row.dataset.id = entry.id;

    // Icon ring
    var iconRing = document.createElement('div');
    iconRing.className = 'file-item-icon';
    iconRing.setAttribute('aria-hidden', 'true');
    // Static SVG assigned by type — no user content
    var iconColors = { pdf: '#991B1B', docx: '#1E3A8A', img: '#065F46', unknown: '#6B7280' };
    var iconBgs    = { pdf: '#FEE2E2', docx: '#DBEAFE', img: '#D1FAE5', unknown: '#F3F4F6' };
    iconRing.style.background = iconBgs[type] || iconBgs.unknown;
    iconRing.style.color       = iconColors[type] || iconColors.unknown;

    var iconSvgs = {
      pdf:  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>',
      docx: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
      img:  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
      unknown: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>',
    };
    // SECURITY: static SVG strings, no user data
    iconRing.innerHTML = iconSvgs[type] || iconSvgs.unknown;

    // Name + meta wrapper
    var infoEl = document.createElement('div');
    infoEl.className = 'file-item-info';
    infoEl.style.cssText = 'flex:1; min-width:0; display:flex; flex-direction:column; gap:0.15rem;';

    var nameEl = document.createElement('div');
    nameEl.className = 'file-item-name';
    nameEl.textContent = entry.file.name;    // textContent — safe for user filenames

    var metaEl = document.createElement('div');
    metaEl.className = 'file-item-meta';
    metaEl.style.display = 'flex';
    metaEl.style.alignItems = 'center';

    var sizeEl = document.createElement('span');
    sizeEl.textContent = formatBytes(entry.file.size);
    metaEl.appendChild(sizeEl);

    // PDF page estimate — async scan of file bytes
    if (type === 'pdf') {
      var pageEstEl = document.createElement('span');
      pageEstEl.className = 'file-item-pages';
      pageEstEl.textContent = '';
      metaEl.appendChild(pageEstEl);

      estimatePdfPages(entry.file, function (count) {
        if (count !== null) {
          pageEstEl.textContent = '\u00b7 ~' + count + (count === 1 ? ' page' : ' pages');
        }
      });
    }

    infoEl.appendChild(nameEl);
    infoEl.appendChild(metaEl);

    // Remove button
    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'file-item-remove';
    removeBtn.setAttribute('aria-label', 'Remove ' + entry.file.name);
    // SECURITY: static SVG
    removeBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    removeBtn.addEventListener('click', function () { removeFile(entry.id); });

    row.appendChild(iconRing);
    row.appendChild(infoEl);
    row.appendChild(removeBtn);
    return row;
  }

  /**
   * Estimate PDF page count by scanning the first PDF_SCAN_BYTES bytes for
   * occurrences of "/Type /Page" (excluding "/Pages" dictionary entries).
   * Calls back with an integer count or null if the scan is inconclusive.
   */
  function estimatePdfPages(file, callback) {
    var slice = file.slice(0, PDF_SCAN_BYTES);
    var reader = new FileReader();
    reader.onload = function (e) {
      try {
        var bytes = new Uint8Array(e.target.result);
        // Decode as Latin-1 so byte values are preserved (PDF is binary)
        var text = '';
        for (var i = 0; i < bytes.length; i++) {
          text += String.fromCharCode(bytes[i]);
        }
        // Match "/Type /Page" but not "/Type /Pages" (the parent node)
        var matches = text.match(/\/Type\s*\/Page[^s]/g);
        callback(matches ? matches.length : null);
      } catch (_) {
        callback(null);
      }
    };
    reader.onerror = function () { callback(null); };
    reader.readAsArrayBuffer(slice);
  }

  function renderFileList() {
    while (fileListEl.firstChild) { fileListEl.removeChild(fileListEl.firstChild); }
    fileList.forEach(function (entry, i) {
      var row = buildFileRow(entry);
      row.classList.add('animate-fade-in-up');
      row.style.animationDelay = (i * 0.05) + 's';
      fileListEl.appendChild(row);
    });
    fileListSection.style.display = fileList.length > 0 ? '' : 'none';
    updateSizeBar();
    updateSubmitState();
  }

  // ── File management ──────────────────────────────────────────────────────────

  function addFiles(newFiles) {
    clearError();
    var rejected = [];
    var added    = 0;

    for (var i = 0; i < newFiles.length; i++) {
      var f = newFiles[i];

      // EDGE CASE: max 20 files total
      if (fileList.length >= MAX_FILES) {
        showError('That\'s more than ' + MAX_FILES + ' files — remove a few and try again!');
        break;
      }

      if (!isAccepted(f)) {
        rejected.push(f.name);
        continue;
      }

      // EDGE CASE: 0-byte file
      if (f.size === 0) {
        rejected.push(f.name + ' (empty file)');
        continue;
      }

      fileList.push({ id: nextId++, file: f });
      added++;
    }

    if (rejected.length > 0) {
      var names = rejected.slice(0, 3).join(', ');
      var extra = rejected.length > 3 ? ' and ' + (rejected.length - 3) + ' more' : '';
      showError('We can\'t print ' + names + extra + ' — we accept PDF, Word docs (.docx), JPG, and PNG files.');
    }

    if (added > 0) {
      renderFileList();
    }
  }

  function removeFile(id) {
    fileList = fileList.filter(function (e) { return e.id !== id; });
    renderFileList();
    if (fileList.length === 0) { clearError(); }
  }

  // ── Form submission — XHR for upload progress tracking ──────────────────────

  /**
   * Browsers don't allow setting input.files directly (it's read-only).
   * We work around this by constructing a DataTransfer and assigning its files.
   */
  function syncInputFiles() {
    try {
      var dt = new DataTransfer();
      fileList.forEach(function (entry) { dt.items.add(entry.file); });
      fileInput.files = dt.files;
    } catch (e) {
      // FALLBACK: DataTransfer not supported (very old browsers).
      console.warn('upload.js: DataTransfer not supported, fallback to native input.');
    }
  }

  /**
   * Parse an XHR error response and surface it to the user.
   * Resets the progress bar and restores the submit button.
   */
  function handleXhrError(xhr) {
    progressWrap.style.display = 'none';
    progressWrap.classList.remove('upload-progress-active');
    progressBar.style.width = '0%';
    resetSubmitButton();
    updateSubmitState();

    var friendlyMsg = 'Oops! We couldn\'t upload your files. Please try again.';
    var technicalDetail = '';
    try {
      var data = JSON.parse(xhr.responseText);
      if (data && data.detail) { technicalDetail = data.detail; }
    } catch (_) {}
    showError(friendlyMsg + (technicalDetail ? ' (' + technicalDetail + ')' : ''));
  }

  form.addEventListener('submit', function (evt) {
    evt.preventDefault();   // always intercept — we upload via XHR
    if (fileList.length === 0 || totalBytes() > MAX_TOTAL_BYTES) { return; }

    syncInputFiles();
    setSubmitLoading();

    progressWrap.style.display = '';
    progressWrap.classList.add('upload-progress-active');
    progressBar.style.width = '8%';
    var progressLabel = document.getElementById('upload-progress-label');
    if (progressLabel) progressLabel.textContent = 'Uploading your files\u2026';

    setTimeout(function () {
      if (parseFloat(progressBar.style.width) <= 10) {
        progressBar.style.width = '15%';
      }
    }, 300);

    var xhr = new XMLHttpRequest();
    var formData = new FormData(form);

    xhr.upload.addEventListener('progress', function (e) {
      if (e.lengthComputable) {
        // Never go below 15% — phantom progress already claimed that floor
        var pct = Math.max(15, Math.round((e.loaded / e.total) * 100));
        progressBar.style.width = pct + '%';
      }
    });

    xhr.addEventListener('load', function () {
      if (xhr.status >= 200 && xhr.status < 400) {
        progressBar.style.width = '100%';
        progressWrap.classList.remove('upload-progress-active');
        var progressLabel = document.getElementById('upload-progress-label');
        if (progressLabel) {
          progressLabel.textContent = 'Upload complete! Redirecting\u2026';
          progressLabel.style.color = 'var(--success)';
        }
        var dest = xhr.responseURL || '/';
        setTimeout(function () { window.location.href = dest; }, 400);
      } else {
        handleXhrError(xhr);
      }
    });

    xhr.addEventListener('error', function () {
      handleXhrError(xhr);
      showError('Oops! We couldn\'t upload that. Please check your internet connection and try again.');
    });

    xhr.addEventListener('abort', function () {
      progressWrap.style.display = 'none';
      progressWrap.classList.remove('upload-progress-active');
      progressBar.style.width = '0%';
      resetSubmitButton();
      updateSubmitState();
    });

    xhr.open('POST', form.action);
    xhr.send(formData);
  });

  // ── Drop zone interactions ───────────────────────────────────────────────────

  dropZone.addEventListener('click', function (evt) {
    if (evt.target === browseTrigger || browseTrigger.contains(evt.target)) { return; }
    fileInput.click();
  });

  browseTrigger.addEventListener('click', function (evt) {
    evt.stopPropagation();
    fileInput.click();
  });

  dropZone.addEventListener('keydown', function (evt) {
    if (evt.key === 'Enter' || evt.key === ' ') {
      evt.preventDefault();
      fileInput.click();
    }
  });

  dropZone.addEventListener('dragover', function (evt) {
    evt.preventDefault();
    dropZone.classList.add('is-dragging');
  });
  dropZone.addEventListener('dragleave', function (evt) {
    // Only remove if truly leaving the drop zone, not a child element
    if (!dropZone.contains(evt.relatedTarget)) {
      dropZone.classList.remove('is-dragging');
    }
  });
  dropZone.addEventListener('drop', function (evt) {
    evt.preventDefault();
    dropZone.classList.remove('is-dragging');
    var dropped = evt.dataTransfer && evt.dataTransfer.files;
    if (dropped && dropped.length > 0) {
      addFiles(dropped);
    }
  });

  fileInput.addEventListener('change', function () {
    if (fileInput.files && fileInput.files.length > 0) {
      addFiles(fileInput.files);
      // Reset so re-selecting the same file triggers change again
      fileInput.value = '';
    }
  });

  clearAllBtn.addEventListener('click', function () {
    fileList = [];
    renderFileList();
    clearError();
  });

  // ── Init ─────────────────────────────────────────────────────────────────────
  renderFileList();

}());
