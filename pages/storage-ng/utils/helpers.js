/**
 * Helpers - pure formatting/escaping utilities (no store imports; the
 * formatting core is DOM-free so it can be unit-tested under node).
 *
 * @module utils/helpers
 */

/**
 * Coerce any row value to a finite non-negative number. Symbol/BigInt throw
 * on Number(), and hostile magnitudes make toFixed emit exponent form
 * ("1e+296 TB") — anything unusable becomes 0, anything huge is capped.
 * @param {*} v
 * @param {number} cap
 * @returns {number}
 */
function toSafeNumber(v, cap) {
  let n;
  try { n = Number(v) || 0; } catch { return 0; }
  if (!Number.isFinite(n) || n < 0) return 0;
  return Math.min(n, cap);
}

/**
 * Format a byte count as a human-readable storage size.
 *
 * Storage units use the decimal base 1000 and the smallest displayed unit is
 * MB: byte and KB magnitudes never appear (sub-MB values render as "0.1 MB").
 * @param {number} bytes
 * @returns {string}
 */
export function formatSize(bytes) {
  const n = toSafeNumber(bytes, 1e15);
  if (n <= 0) return '0 MB';
  const KB = 1000;
  const MB = KB * 1000;
  const GB = MB * 1000;
  const TB = GB * 1000;
  if (n >= TB) return `${(n / TB).toFixed(2)} TB`;
  if (n >= GB) return `${(n / GB).toFixed(2)} GB`;
  return `${Math.max(n / MB, 0.1).toFixed(1)} MB`;
}

/**
 * Format a transfer rate (bytes/s) as bandwidth with the binary base 1024.
 * The smallest displayed unit is MB/s; KB/s and below never appear.
 * @param {number} bytesPerSecond
 * @returns {string}
 */
export function formatRate(bytesPerSecond) {
  const v = toSafeNumber(bytesPerSecond, 1e12);
  if (v <= 0) return '0 MB/s';
  const MB = 1024 * 1024;
  const GB = MB * 1024;
  if (v >= GB) return `${(v / GB).toFixed(2)} GB/s`;
  return `${Math.max(v / MB, 0.1).toFixed(1)} MB/s`;
}

/**
 * Format a duration (seconds) using only 时/分/秒 — milliseconds never appear.
 * @param {number} seconds
 * @returns {string}
 */
export function formatDuration(seconds) {
  const total = Math.round(toSafeNumber(seconds, 1e9));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}时${String(m).padStart(2, '0')}分${String(s).padStart(2, '0')}秒`;
  if (m) return s ? `${m}分${String(s).padStart(2, '0')}秒` : `${m}分`;
  return `${s}秒`;
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
 *
 * The textContent round-trip covers & < >; attribute contexts (value=,
 * title=, data-*) additionally need the quote characters escaped
 * (OWASP XSS Prevention, HTML Attribute Context), so quotes are
 * re-escaped explicitly afterwards.
 * @param {string} str
 * @returns {string}
 */
export function escapeHtml(str) {
  if (!str) return '';
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
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

/**
 * Sandbox-safe window.open (MDN 惯例: 返回 null 即被拦截, 必须检查).
 *
 * 宿主 iframe 的 sandbox 是 allow-scripts allow-forms allow-downloads,
 * 没有 allow-popups, window.open 恒返回 null — 裸调用会静默失败, 用户
 * 以为点了没反应。此助手检测拦截并给出可见反馈: 降级为复制链接到剪贴板。
 * @param {string} url
 * @returns {Promise<boolean>} true=已打开新窗口; false=被拦截(已降级复制)
 */
export async function openExternal(url) {
  if (!url) return false;
  let win = null;
  try {
    win = window.open(url, '_blank', 'noopener');
  } catch {
    win = null;
  }
  if (win) return true;
  await copyToClipboard(url);
  const { toast } = await import('../components/toast.js');
  toast('沙箱内无法弹出新窗口，链接已复制到剪贴板', 'warn');
  return false;
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

/**
 * Sequential per-item runner with failure collection (no early stop) —
 * the shared shape for per-row request loops (downloads, deletes,
 * removals) so one bad item surfaces in the summary instead of silently
 * dropping the rest.
 * @param {Array} items
 * @param {(item: any, index: number) => Promise<void>} fn
 * @returns {Promise<{done: number, failed: string[]}>} failed entries are
 *   "label: message" with the item's name/id when available
 */
export async function runEachWithFailures(items, fn) {
  const failed = [];
  let done = 0;
  for (let i = 0; i < items.length; i++) {
    try {
      await fn(items[i], i);
      done++;
    } catch (e) {
      const label = (items[i] && (items[i].name || items[i].id)) || i;
      failed.push(`${label}: ${(e && e.message) || e}`);
    }
  }
  return { done, failed };
}