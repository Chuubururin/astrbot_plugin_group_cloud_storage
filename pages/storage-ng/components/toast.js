/**
 * Toast - lightweight notifications (I14).
 *
 * Auto-dismisses; icons only (zero emoji). The host is a sandboxed
 * iframe, so the toast DOM is appended to document.body here - the
 * host postMessage bridge never sees it.
 *
 * @module components/toast
 */

import { getIcon } from '../icons.js';
import { TOAST_DURATION } from '../constants.js';

let container = null;

function ensure() {
  if (container) return;
  container = document.createElement('div');
  container.className = 'toast-container';
  document.body.appendChild(container);
}

/**
 * Show a toast message.
 * @param {string} message
 * @param {'info'|'success'|'warn'|'error'} [variant='info']
 * @param {number} [duration=TOAST_DURATION] - display time in ms
 */
export function toast(message, variant = 'info', duration = TOAST_DURATION) {
  ensure();
  const el = document.createElement('div');
  el.className = `toast toast-${variant}`;

  const iconMap = { info: 'INFO', success: 'CHECK', warn: 'ALERT', error: 'X' };
  const iconSpan = document.createElement('span');
  iconSpan.className = 'toast-icon';
  iconSpan.innerHTML = getIcon(iconMap[variant] || 'INFO', 14);

  const msgSpan = document.createElement('span');
  msgSpan.className = 'toast-msg';
  msgSpan.textContent = message;

  el.appendChild(iconSpan);
  el.appendChild(msgSpan);
  container.appendChild(el);

  requestAnimationFrame(() => el.classList.add('toast-show'));

  setTimeout(() => {
    el.classList.remove('toast-show');
    el.classList.add('toast-hide');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }, duration);
}