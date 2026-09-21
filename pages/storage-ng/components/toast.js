/**
 * Toast - lightweight notifications (I14).
 *
 * Auto-dismisses; icons only (zero emoji). The host is a sandboxed
 * iframe, so the toast DOM is appended to document.body here - the
 * host postMessage bridge never sees it.
 *
 * Teardown is two-stage: the .toast-hide keyframes (styles/components.css)
 * reclaim the node via animationend, and a timer backstops that so a missed
 * event (reduced motion, background tab) can never leave dead nodes
 * occupying the stack.
 *
 * @module components/toast
 */

import { getIcon } from '../icons.js';
import { TOAST_DURATION } from '../constants.js';

/** Hard cap on the visible stack: excess toasts are dropped oldest-first. */
const MAX_TOASTS = 5;
/** Identical text+variant inside this window merges instead of stacking. */
const DEDUPE_WINDOW_MS = 1200;
/** Fade-out duration; must match .toast-hide in styles/components.css. */
const FADE_MS = 200;

let container = null;
/** key -> { el, at } of the live toasts (dedupe + eviction bookkeeping). */
const active = new Map();

function ensure() {
  if (container) return;
  container = document.createElement('div');
  container.className = 'toast-container';
  document.body.appendChild(container);
}

/** Detach one toast and forget it (idempotent: safe from both paths). */
function drop(el) {
  if (!el) return;
  clearTimeout(el.__timer);
  if (el.__key) active.delete(el.__key);
  el.remove();
}

/** Start the fade-out; the node is removed on animationend or by the backstop. */
function dismiss(el) {
  el.classList.remove('toast-show');
  el.classList.add('toast-hide');
  el.addEventListener('animationend', () => drop(el), { once: true });
  // Backstop: animationend never fires when the animation does not run.
  setTimeout(() => drop(el), FADE_MS + 300);
}

/**
 * Show a toast message.
 * @param {string} message
 * @param {'info'|'success'|'warn'|'error'} [variant='info']
 * @param {number} [duration=TOAST_DURATION] - display time in ms
 */
export function toast(message, variant = 'info', duration = TOAST_DURATION) {
  ensure();
  const key = `${variant}\n${message}`;
  const prev = active.get(key);
  // F-extra: an identical toast still on screen absorbs the repeat (its
  // timer restarts) instead of stacking a second, indistinguishable node.
  if (prev && prev.el.isConnected && Date.now() - prev.at < DEDUPE_WINDOW_MS) {
    prev.at = Date.now();
    clearTimeout(prev.el.__timer);
    prev.el.__timer = setTimeout(() => dismiss(prev.el), duration);
    return;
  }

  const el = document.createElement('div');
  el.className = `toast toast-${variant}`;
  el.__key = key;

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
  active.set(key, { el, at: Date.now() });

  // Defense in depth: keep the stack bounded so accumulated (fading) nodes
  // can never push a fresh toast out of the viewport.
  while (container.children.length > MAX_TOASTS) {
    const oldest = container.firstElementChild;
    if (!oldest || oldest === el) break;
    drop(oldest);
  }

  requestAnimationFrame(() => el.classList.add('toast-show'));
  el.__timer = setTimeout(() => dismiss(el), duration);
}
