/**
 * Text viewer - essence full-text display (F12).
 *
 * Scrollable pre-formatted text with a segment-rebuild note: a logical
 * long text reassembled from shards reports any missing parts so the
 * user knows the rebuild is incomplete. Sandbox-safe overlay.
 *
 * @module components/text-viewer
 */

let overlay = null;

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
  const close = () => overlay.classList.add('hidden');
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
  overlay.classList.remove('hidden');
}