/**
 * static/js/toast.js
 *
 * Lightweight toast notification system for PrintBot.
 * Appends toast elements to #toast-container, auto-removes after 4s.
 * No external dependencies. No innerHTML for user content.
 *
 * Usage: showToast('Message text', 'success' | 'error' | 'info')
 */

(function () {
  'use strict';

  var ICONS = {
    success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };

  window.showToast = function showToast(message, type) {
    type = type || 'info';

    var container = document.getElementById('toast-container');
    if (!container) return;

    var el = document.createElement('div');
    el.className = 'toast toast-' + type;

    var iconWrap = document.createElement('span');
    iconWrap.style.cssText = 'flex-shrink:0;display:flex;align-items:center;';
    iconWrap.innerHTML = ICONS[type] || ICONS.info;

    var textEl = document.createElement('span');
    textEl.textContent = message;

    el.appendChild(iconWrap);
    el.appendChild(textEl);

    container.appendChild(el);

    setTimeout(function () {
      el.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 320);
    }, 4000);
  };
}());
