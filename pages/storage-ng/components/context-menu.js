/**
 * Context menu - right-click menu for list rows.
 *
 * Two entry points:
 *   show(x, y, source, item, opts)  — command-registry-driven (file/album/essence/netdisk rows)
 *   showRaw(x, y, items, onAction)  — arbitrary item array (group rows, folder rows, etc.)
 *
 * Singleton DOM element; closes on click outside, Escape, scroll,
 * or after an action fires. Keyboard: ↑↓ navigate, Enter/Space activates.
 *
 * @module components/context-menu
 */

import { getIcon } from '../icons.js';
import { commands, canRunRowAware } from '../features/commands.js';
import { getState } from '../store.js';

/** @type {HTMLElement|null} */
let menuEl = null;

function ensureMenu() {
  if (menuEl) return menuEl;
  menuEl = document.createElement('div');
  menuEl.className = 'ctx-menu hidden';
  menuEl.setAttribute('role', 'menu');
  document.body.appendChild(menuEl);

  document.addEventListener('click', hide);
  document.addEventListener('scroll', hide, true);
  document.addEventListener('keydown', globalKeyHandler);
  return menuEl;
}

/** Global key handler: Escape closes, ↑↓ navigate, Enter/Space activates. */
function globalKeyHandler(e) {
  if (!menuEl || menuEl.classList.contains('hidden')) return;
  if (e.key === 'Escape') { hide(); return; }
  const btns = Array.from(menuEl.querySelectorAll('.ctx-item:not([data-disabled])'));
  if (!btns.length) return;
  const cur = menuEl.querySelector('.ctx-item:focus');
  const idx = cur ? btns.indexOf(cur) : -1;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    btns[(idx + 1) % btns.length].focus();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    btns[(idx - 1 + btns.length) % btns.length].focus();
  } else if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    if (cur && !cur.dataset.disabled) cur.click();
  }
}

export function hide() {
  if (menuEl) menuEl.classList.add('hidden');
}

// ─── Raw API (arbitrary items) ───────────────────────────────────────

/**
 * Show a context menu from an arbitrary item array.
 *
 * @param {number} x - client X
 * @param {number} y - client Y
 * @param {Array<{id:string, label:string, icon?:string, danger?:boolean,
 *   disabled?:boolean, title?:string, sep?:boolean}>} items
 * @param {(id:string) => void} onAction
 */
export function showRaw(x, y, items, onAction) {
  const el = ensureMenu();
  if (!items.length) { hide(); return; }

  el.innerHTML = items.map((mi) => {
    if (mi.sep) return '<div class="ctx-sep"></div>';
    const cls = [
      'ctx-item',
      mi.danger ? 'danger' : '',
    ].filter(Boolean).join(' ');
    const icon = mi.icon ? `<span class="ctx-icon">${getIcon(mi.icon, 13)}</span>` : '';
    const attrs = [
      `data-cmd="${mi.id}"`,
      'role="menuitem"',
      mi.disabled ? 'data-disabled tabindex="-1"' : 'tabindex="-1"',
      mi.title ? `title="${mi.title}"` : '',
    ].filter(Boolean).join(' ');
    return `<button class="${cls}" ${attrs}>${icon}<span class="ctx-label">${mi.label}</span></button>`;
  }).join('');

  bindClicks(el, onAction);
  position(el, x, y);
  el.querySelector('.ctx-item:not([data-disabled])')?.focus();
}

// ─── Command-registry API (file rows) ────────────────────────────────

/**
 * Show context menu for a file/album/essence/netdisk row.
 * Items are derived from the source's capabilities + command registry.
 *
 * @param {number} x
 * @param {number} y
 * @param {Object} source - DataSource adapter
 * @param {Object} item - row data
 * @param {Object} [opts]
 * @param {Set} [opts.selection]
 * @param {(cmdId:string, ctx:Object) => void} [opts.onAction]
 */
export function show(x, y, source, item, opts = {}) {
  const rows = opts.selection?.size > 0
    ? getSelectedRows(source, opts.selection)
    : [item];

  const env = {
    count: rows.length,
    hasGroup: Boolean(item?.group_id),
    rowsHaveGroup: rows.some((r) => r.group_id),
  };

  const allCmds = commands();
  const caps = source.capabilities || [];
  const items = [];
  for (const capId of caps) {
    const cmd = allCmds[capId];
    if (!cmd || capId === 'clear') continue;
    const check = canRunRowAware(capId, env);
    items.push({
      id: capId,
      label: cmd.label || capId,
      icon: cmd.icon || null,
      danger: Boolean(cmd.danger),
      disabled: !check.ok,
      title: check.ok ? '' : (check.reason || ''),
    });
  }

  if (!items.length) { hide(); return; }
  showRaw(x, y, items, (cmdId) => {
    if (opts.onAction) opts.onAction(cmdId, { source, row: item, rows });
  });
}

// ─── Internals ───────────────────────────────────────────────────────

function getSelectedRows(source, selection) {
  const itemsKey = source.itemsKey || 'fileItems';
  const allItems = getState()[itemsKey] || [];
  return allItems.filter((r) => selection.has(source.rowKey(r)));
}

function bindClicks(el, onAction) {
  el.querySelectorAll('.ctx-item').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (btn.dataset.disabled !== undefined) return;
      hide();
      onAction(btn.dataset.cmd);
    });
  });
}

function position(el, x, y) {
  el.classList.remove('hidden');
  const rect = el.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  el.style.left = `${x + rect.width > vw ? vw - rect.width - 4 : x}px`;
  el.style.top = `${y + rect.height > vh ? vh - rect.height - 4 : y}px`;
}
