/**
 * static/js/upload.js — Multi-file drag-and-drop upload handler.
 *
 * Responsibilities:
 *  - Manage a local file list (add / remove entries)
 *  - Validate: accepted types, 90 MB total, max 20 files
 *  - Render file list rows with type icon, name, size, remove button
 *  - Show live total-size progress bar (turns red over limit)
 *  - Enable/disable the Submit button based on state
 *  - Show inline error messages without alert()
 *
 * No external dependencies. Uses textContent (never innerHTML) for user data.
 */

(function () {
  'use strict';

  // ── Constants ────────────────────────────────────────────────────────────────
  var MAX_TOTAL_BYTES = 90 * 1024 * 1024;   // 90 MB
  var MAX_FILES       = 20;

  var ACCEPTED_MIME = [
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'image/jpeg',
    'image/png',
  ];
  var ACCEPTED_EXT = ['pdf', 'docx', 'doc', 'jpg', 'jpeg', 'png'];

  // ── DOM references ───────────────────────────────────────────────────────────
  var form            = document.getElementById('upload-form');
  var dropZone        = document.getElementById('drop-zone');
  var browseTrigger   = document.getElementById('dz-browse-trigger');
  var fileInput       = document.getElementById('file-input');
  var fileListSection = document.getElementById('file-list-section');
  var fileListEl      = document.getElementById('file-list');
  var clearAllBtn     = document.getElementById('clear-all-btn');
  var submitBtn       = document.getElementById('submit-btn');
  var sizeBar         = document.getElementById('size-bar');
  var sizeUsedLabel   = document.getElementById('size-used-label');
  var sizeOverMsg     = document.getElementById('size-over-msg');
  var fileCountNote   = document.getElementById('file-count-note');
  var uploadErrorEl   = document.getElementById('upload-error');

  // ── Internal state ───────────────────────────────────────────────────────────
  // Each entry: { id: number, file: File, key: string }
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
    // Build alert div safely
    while (uploadErrorEl.firstChild) { uploadErrorEl.removeChild(uploadErrorEl.firstChild); }
    var alert = document.createElement('div');
    alert.className = 'alert alert-error';
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
    icon.innerHTML = '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>';
    var text = document.createElement('span');
    text.textContent = msg;
    alert.appendChild(icon);
    alert.appendChild(text);
    uploadErrorEl.appendChild(alert);

    // Auto-dismiss after 5s
    setTimeout(function () {
      uploadErrorEl.style.display = 'none';
    }, 5000);
  }

  function clearError() {
    uploadErrorEl.style.display = 'none';
  }

  // ── UI rendering ─────────────────────────────────────────────────────────────

  function updateSizeBar() {
    var used  = totalBytes();
    var pct   = Math.min((used / MAX_TOTAL_BYTES) * 100, 100);
    var isOver = used > MAX_TOTAL_BYTES;

    sizeBar.style.width = pct + '%';
    sizeBar.classList.toggle('is-over', isOver);
    sizeBar.classList.toggle('is-full', pct >= 99.9 && !isOver);

    sizeUsedLabel.textContent = formatBytes(used);
    sizeUsedLabel.classList.toggle('size-over', isOver);
    sizeOverMsg.style.display = isOver ? '' : 'none';

    var n = fileList.length;
    fileCountNote.textContent = n === 1 ? '1 file' : n + ' files';
  }

  function updateSubmitState() {
    var valid = fileList.length > 0 && totalBytes() <= MAX_TOTAL_BYTES;
    submitBtn.disabled = !valid;
    submitBtn.setAttribute('aria-disabled', valid ? 'false' : 'true');
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

    // Name
    var nameEl = document.createElement('div');
    nameEl.className = 'file-item-name';
    nameEl.textContent = entry.file.name;    // textContent — safe for user filenames

    // Meta (size)
    var metaEl = document.createElement('div');
    metaEl.className = 'file-item-meta';
    metaEl.textContent = formatBytes(entry.file.size);

    // Remove button
    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'file-item-remove';
    removeBtn.setAttribute('aria-label', 'Remove ' + entry.file.name);
    // Static SVG
    removeBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    removeBtn.addEventListener('click', function () { removeFile(entry.id); });

    row.appendChild(iconRing);
    row.appendChild(nameEl);
    row.appendChild(metaEl);
    row.appendChild(removeBtn);
    return row;
  }

  function renderFileList() {
    while (fileListEl.firstChild) { fileListEl.removeChild(fileListEl.firstChild); }
    fileList.forEach(function (entry) {
      fileListEl.appendChild(buildFileRow(entry));
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
        showError('Maximum ' + MAX_FILES + ' files allowed.');
        break;
      }

      if (!isAccepted(f)) {
        rejected.push(f.name);
        continue;
      }

      // EDGE CASE: 0-byte file
      if (f.size === 0) {
        rejected.push(f.name + ' (empty)');
        continue;
      }

      // Check if this exact file (name + size + lastModified) is already in the list
      var isDuplicate = fileList.some(function (entry) {
        return entry.file.name === f.name
          && entry.file.size === f.size
          && entry.file.lastModified === f.lastModified;
      });
      // EDGE CASE: duplicates are allowed per spec — just warn
      // (different UUIDs on upload, original names preserved server-side)

      fileList.push({ id: nextId++, file: f });
      added++;
    }

    if (rejected.length > 0) {
      var names = rejected.slice(0, 3).join(', ');
      var extra = rejected.length > 3 ? ' and ' + (rejected.length - 3) + ' more' : '';
      showError('Skipped: ' + names + extra + '. Only PDF, DOCX, JPG, PNG files are accepted.');
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

  // ── Form submission — build a real FileList for the <input> ─────────────────

  /**
   * Browsers don't allow setting input.files directly (it's read-only).
   * We work around this by constructing a DataTransfer and assigning its files.
   * This is supported in all modern browsers.
   */
  function syncInputFiles() {
    try {
      var dt = new DataTransfer();
      fileList.forEach(function (entry) { dt.items.add(entry.file); });
      fileInput.files = dt.files;
    } catch (e) {
      // FALLBACK: DataTransfer not supported (very old browsers).
      // The form will submit without files — server will reject gracefully.
      console.warn('upload.js: DataTransfer not supported, fallback to native input.');
    }
  }

  form.addEventListener('submit', function (evt) {
    if (fileList.length === 0 || totalBytes() > MAX_TOTAL_BYTES) {
      evt.preventDefault();
      return;
    }

    // Sync our managed file list into the hidden file input before submission
    syncInputFiles();

    // Show loading state on the button
    submitBtn.disabled = true;
    while (submitBtn.firstChild) { submitBtn.removeChild(submitBtn.firstChild); }
    var spinner = document.createElement('span');
    spinner.className = 'spinner spinner-sm';
    spinner.setAttribute('aria-hidden', 'true');
    var label = document.createElement('span');
    label.textContent = 'Uploading…';
    submitBtn.appendChild(spinner);
    submitBtn.appendChild(label);
  });

  // ── Drop zone interactions ───────────────────────────────────────────────────

  // Click on zone → trigger file picker (except when clicking the browse link itself)
  dropZone.addEventListener('click', function (evt) {
    // The browse-trigger has its own handler below; this catches clicks on the rest
    if (evt.target === browseTrigger || browseTrigger.contains(evt.target)) { return; }
    fileInput.click();
  });

  browseTrigger.addEventListener('click', function (evt) {
    evt.stopPropagation();
    fileInput.click();
  });

  // Keyboard: Enter/Space on drop zone
  dropZone.addEventListener('keydown', function (evt) {
    if (evt.key === 'Enter' || evt.key === ' ') {
      evt.preventDefault();
      fileInput.click();
    }
  });

  // Drag-over: add visual cue
  dropZone.addEventListener('dragover', function (evt) {
    evt.preventDefault();
    dropZone.classList.add('is-dragging');
  });
  dropZone.addEventListener('dragleave', function (evt) {
    // Only remove if leaving the zone itself, not a child element
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

  // File input change (from file picker dialog)
  fileInput.addEventListener('change', function () {
    if (fileInput.files && fileInput.files.length > 0) {
      addFiles(fileInput.files);
      // Reset the input so re-selecting the same file triggers change again
      fileInput.value = '';
    }
  });

  // Clear all button
  clearAllBtn.addEventListener('click', function () {
    fileList = [];
    renderFileList();
    clearError();
  });

  // ── Init ─────────────────────────────────────────────────────────────────────
  renderFileList();

}());
