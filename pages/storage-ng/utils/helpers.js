/**
 * Helpers - pure formatting/escaping utilities (no store imports; the
 * formatting core is DOM-free so it can be unit-tested under node).
 *
 * @module utils/helpers
 */

/**
 * Format a byte count as a human-readable string.
 * @param {number} bytes
 * @returns {string}
 */
export function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '-';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/**
 * Format a timestamp compactly (month/day hour:minute).
 * Accepts unix seconds or an ISO string.
 * @param {number|string} ts
 * @returns {string}
 */
export function formatTime(ts) {
  if (!ts) return '-';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/**
 * Format a timestamp with the year (used in ledgers/detail views).
 * @param {number|string} ts
 * @returns {string}
 */
export function formatTimeFull(ts) {
  if (!ts) return '-';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

/**
 * Escape HTML to prevent XSS from user-controlled strings.
 * @param {string} str
 * @returns {string}
 */
export function escapeHtml(str) {
  if (!str) return '';
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}

/**
 * Debounce a function (used for search inputs, FE pacing).
 * @param {function} fn
 * @param {number} ms
 * @returns {function} debounced wrapper
 */
export function debounce(fn, ms) {
  let timer;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

/**
 * Truncate a string with an ellipsis.
 * @param {string} str
 * @param {number} [max=40]
 * @returns {string}
 */
export function truncate(str, max = 40) {
  if (!str || str.length <= max) return str || '';
  return str.slice(0, max - 1) + '\u2026';
}

/**
 * Join class names, dropping falsy entries.
 * @param {...string} names
 * @returns {string}
 */
export function cls(...names) {
  return names.filter(Boolean).join(' ');
}

/** Copy text to the clipboard with a fallback for insecure contexts. */
export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}