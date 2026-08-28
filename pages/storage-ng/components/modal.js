import { escapeHtml } from '../utils/helpers.js';

/**
 * Modal - sandbox-safe dialogs (F1-F4/F20/D5).
 *
 * The plugin page runs in a sandboxed iframe where native alert/confirm/
 * prompt are blocked, so every dialog is drawn here: confirm (text),
 * prompt (single input), form (label+field rows), detail (key/value grid).
 * Returns a Promise; resolves null on cancel.
 *
 * @module components/modal
 */

let overlay = null;
let box = null;
let resolveCurrent = null;
let lastFocused = null;

/** Lazily build the modal DOM (shared by all dialog types). */
function ensure() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title"></div>
      <div class="modal-body"></div>
      <div class="modal-form hidden"></div>
      <div class="modal-actions">
        <button class="modal-cancel">取消</button>
        <button class="modal-ok primary">确定</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  box = overlay.querySelector('.modal-box');

  overlay.querySelector('.modal-cancel').addEventListener('click', () => close(null));
  overlay.querySelector('.modal-ok').addEventListener('click', () => {
    const form = overlay.querySelector('.modal-form');
    if (!form.classList.contains('hidden')) {
      const inputs = form.querySelectorAll('input, textarea, select');
      const result = {};
      inputs.forEach((inp) => { result[inp.name || inp.id] = inp.value; });
      close(result);
    } else {
      close(true);
    }
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close(null);
  });
  document.addEventListener('keydown', (e) => {
    if (overlay.classList.contains('hidden')) return;
    if (e.key === 'Escape') close(null);
    if (e.key === 'Enter' && !e.target.matches('textarea')) {
      overlay.querySelector('.modal-ok')?.click();
    }
  });
}

function close(value) {
  overlay.classList.add('hidden');
  if (resolveCurrent) {
    resolveCurrent(value);
    resolveCurrent = null;
  }
  // Restore focus to the element that opened the modal (a11y).
  if (lastFocused && typeof lastFocused.focus === 'function') {
    try { lastFocused.focus(); } catch (e) { /* detached */ }
  }
  lastFocused = null;
}

function open() {
  lastFocused = document.activeElement;
  overlay.classList.remove('hidden');
}

/**
 * Confirm dialog.
 * @param {string} title
 * @param {string} text
 * @param {Object} [opts] - {okText, cancelText, danger}
 * @returns {Promise<boolean|null>} true=ok, null=cancel
 */
export function confirmEx(title, text, opts = {}) {
  ensure();
  const { okText = '确定', cancelText = '取消', danger = false } = opts;
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  body.textContent = text;
  body.classList.toggle('hidden', !text);
  const form = overlay.querySelector('.modal-form');
  form.classList.add('hidden');
  form.innerHTML = '';
  const okBtn = overlay.querySelector('.modal-ok');
  okBtn.textContent = okText;
  okBtn.className = danger ? 'modal-ok danger' : 'modal-ok primary';
  overlay.querySelector('.modal-cancel').textContent = cancelText;
  overlay.querySelector('.modal-cancel').classList.remove('hidden');
  open();
  okBtn.focus();
  return new Promise((r) => { resolveCurrent = r; });
}

/**
 * Prompt dialog (single text input).
 * @returns {Promise<string|null>} entered value, null on cancel
 */
export function promptEx(title, text, opts = {}) {
  ensure();
  const { placeholder = '', value = '', okText = '确定' } = opts;
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  body.textContent = text;
  body.classList.toggle('hidden', !text);
  const form = overlay.querySelector('.modal-form');
  form.classList.remove('hidden');
  form.innerHTML = `<input type="text" name="value" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value || '')}" style="width:100%">`;
  overlay.querySelector('.modal-ok').textContent = okText;
  overlay.querySelector('.modal-ok').className = 'modal-ok primary';
  overlay.querySelector('.modal-cancel').textContent = '取消';
  overlay.querySelector('.modal-cancel').classList.remove('hidden');
  open();
  const inp = form.querySelector('input');
  setTimeout(() => { inp.focus(); inp.select(); }, 50);
  return new Promise((r) => { resolveCurrent = r; }).then((res) => (
    res === null ? null : (res.value ?? null)
  ));
}

/**
 * Detail dialog (key/value grid) - replaces confirmEx-as-text-viewer.
 * @param {string} title
 * @param {Array<{label: string, value: string}>} fields
 * @returns {Promise<boolean|null>}
 */
export function detailEx(title, fields) {
  ensure();
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  body.classList.remove('hidden');
  body.innerHTML = `<div class="detail-grid">${fields.map((f) =>
    `<div class="detail-row"><span class="detail-label">${escapeHtml(f.label)}</span>` +
    `<span class="detail-value">${escapeHtml(f.value == null ? '' : f.value)}</span></div>`
  ).join('')}</div>`;
  const form = overlay.querySelector('.modal-form');
  form.classList.add('hidden');
  form.innerHTML = '';
  const okBtn = overlay.querySelector('.modal-ok');
  okBtn.textContent = '关闭';
  okBtn.className = 'modal-ok primary';
  overlay.querySelector('.modal-cancel').classList.add('hidden');
  open();
  okBtn.focus();
  return new Promise((r) => { resolveCurrent = r; });
}

/**
 * Form dialog - label + field rows.
 *
 * @param {string} title
 * @param {Array<{name: string, label: string, type?: string, value?: string,
 *         placeholder?: string, rows?: number, options?: Array<{value,label}>}>} rows
 * @param {Object} [opts] - {okText}
 * @returns {Promise<Object|null>} map of field name -> string value
 */
export function showFormModal(title, rows, opts = {}) {
  ensure();
  const { okText = '确定' } = opts;
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  body.classList.add('hidden');
  body.textContent = '';
  const form = overlay.querySelector('.modal-form');
  form.classList.remove('hidden');
  form.innerHTML = rows.map((r) => {
    const id = r.name || r.label;
    if (r.type === 'select') {
      const optsHtml = (r.options || []).map((o) =>
        `<option value="${escapeHtml(String(o.value))}" ${o.value === r.value ? 'selected' : ''}>${escapeHtml(o.label)}</option>`
      ).join('');
      return `<label class="form-row"><span>${escapeHtml(r.label)}</span><select name="${escapeHtml(id)}">${optsHtml}</select></label>`;
    }
    if (r.type === 'textarea') {
      return `<label class="form-row"><span>${escapeHtml(r.label)}</span><textarea name="${escapeHtml(id)}" rows="${r.rows || 4}" placeholder="${escapeHtml(r.placeholder || '')}">${escapeHtml(r.value || '')}</textarea></label>`;
    }
    return `<label class="form-row"><span>${escapeHtml(r.label)}</span>` +
      `<input type="${escapeHtml(r.type || 'text')}" name="${escapeHtml(id)}" value="${escapeHtml(r.value == null ? '' : r.value)}" placeholder="${escapeHtml(r.placeholder || '')}"></label>`;
  }).join('');
  overlay.querySelector('.modal-ok').textContent = okText;
  overlay.querySelector('.modal-ok').className = 'modal-ok primary';
  overlay.querySelector('.modal-cancel').textContent = '取消';
  overlay.querySelector('.modal-cancel').classList.remove('hidden');
  open();
  const firstInput = form.querySelector('input, textarea, select');
  if (firstInput) setTimeout(() => firstInput.focus(), 50);
  return new Promise((r) => { resolveCurrent = r; });
}