/**
 * Text viewer - essence full-text display (F12).
 *
 * Scrollable pre-formatted text with a segment-rebuild note: a logical
 * long text reassembled from shards reports any missing parts so the
 * user knows the rebuild is incomplete. Sandbox-safe overlay.
 *
 * The overlay reuses .modal-overlay, so the global shortcut handler
 * (keyboard.js) defers Escape to it: without a listener of its own,
 * Escape over the viewer was a silent no-op.
 *
 * @module components/text-viewer
 */

let overlay = null;
let lastFocused = null;

/** Close the viewer: hide, unbind Escape, restore the opener's focus. */
function close() {
  if (!overlay || overlay.classList.contains('hidden')) return;
  overlay.classList.add('hidden');
  document.removeEventListener('keydown', onKeydown);
  if (lastFocused && typeof lastFocused.focus === 'function') {
    try { lastFocused.focus(); } catch (e) { /* opener detached */ }
  }
  lastFocused = null;
}

/** Escape closes the viewer (the overlay owns the key while open). */
function onKeydown(e) {
  if (e.key !== 'Escape') return;
  e.preventDefault();
  close();
}

function ensure() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.innerHTML = `
    <div class="modal-box modal-wide">
      <div class="modal-title"></div>
      <pre class="essence-text"></pre>
      <div class="modal-actions">
        <button class="modal-ok primary">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-ok').addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
}

/**
 * Show essence full text.
 * @param {string} name - resource name
 * @param {{text?: string, missing?: Array}} data - rebuilt text + missing parts
 */
export function showTextViewer(name, data) {
  ensure();
  overlay.querySelector('.modal-title').textContent = `精华全文: ${name}`;
  const pre = overlay.querySelector('.essence-text');
  const missing = data.missing || [];
  const note = missing.length
    ? `\n\n[缺少分片: ${missing.join(', ')}，全文可能不完整]`
    : '';
  pre.textContent = (data.text || '(空)') + note;
  lastFocused = document.activeElement;
  overlay.classList.remove('hidden');
  // Bound only while open: a lingering listener would swallow Escape on
  // every later screen.
  document.removeEventListener('keydown', onKeydown);
  document.addEventListener('keydown', onKeydown);
  overlay.querySelector('.modal-ok')?.focus();
}
