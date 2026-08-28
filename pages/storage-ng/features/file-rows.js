/**
 * File rows - row building and pagination helpers for the resource table
 * (shared by group files / albums / essence / netdisk, FE-18).
 *
 * Rows follow the file-manager paradigm (N-08): an up-level ".." row for
 * navigating back, folder rows navigate, file rows select on click and
 * open the preview on double-click. Keyed rendering keeps DOM churn low.
 *
 * @module features/file-rows
 */

import { getState, set, refresh } from '../store.js';
import { TYPE_LABELS, API, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { formatSize, formatTime, escapeHtml, copyToClipboard } from '../utils/helpers.js';
import { applyKeyedDiff } from '../utils/dom-diff.js';
import { openPreview } from './preview.js';
import { show as showContextMenu, showRaw } from '../components/context-menu.js';
import { runCommand } from './commands.js';
import { promptEx, confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';

/** CT-9 ext->type lookup map built from the cached classify table. */
export function extTypeMap(table) {
  if (!table || !table.ext_types) return null;
  const map = new Map();
  for (const [type, exts] of Object.entries(table.ext_types)) {
    for (const e of exts || []) map.set(e, type);
  }
  return map;
}

/** Row key, namespaced so folder rows never collide with file ids. */
export function rowKeyOf(source, item) {
  if (item.is_up) return 'dir:..';
  if (source.id === 'group' && (item.is_dir || item.is_folder)) return `dir:${item.id}`;
  return source.rowKey(item);
}

function typeLabel(item) {
  if (item.is_up) return '上一级';
  if (item.is_folder) return '文件夹';
  if (item.type === 'file' || !item.type) return '文件';
  return TYPE_LABELS[item.type] || item.type;
}

/**
 * Build one row: up-level row navigates back; folder rows navigate;
 * file rows select (click) / preview (double-click).
 * @param {Object} source - DataSource adapter
 * @param {Object} item - row data
 * @returns {HTMLElement} <tr> (dataset.key / dataset.dir set)
 */
export function buildRow(source, item) {
  const isFolderRow = Boolean(item.is_dir || item.is_folder);
  const isUp = Boolean(item.is_up);
  const key = rowKeyOf(source, item);
  const tr = document.createElement('tr');
  tr.dataset.key = key;
  tr.dataset.dir = isFolderRow ? '1' : '0';
  const selected = !isFolderRow && source.selection ? source.selection.has(key) : false;
  tr.className = selected ? 'selected clickable' : 'clickable';

  tr.innerHTML = `
    <td class="col-chk">${isFolderRow ? '' : `<input type="checkbox" ${selected ? 'checked' : ''} />`}</td>
    <td class="col-name">
      ${isUp ? getIcon('ARROW_LEFT', 13) : (isFolderRow ? getIcon('FOLDER', 13) : (item.is_volume ? getIcon('FILES', 13) : ''))}
      <span class="fname">${escapeHtml(item.name)}</span>
      ${item.is_volume ? '<span class="badge">分卷</span>' : ''}
      ${item.is_long ? '<span class="badge">长集</span>' : ''}
      ${item.indexed_at ? '<span class="badge">索引</span>' : ''}
      ${item.tags ? `<span class="badge">${escapeHtml(item.tags)}</span>` : ''}
    </td>
    <td><span class="badge ${item.type || 'folder'}">${typeLabel(item)}</span></td>
    <td class="col-size">${item.is_dir ? '-' : formatSize(item.size)}</td>
    <td class="col-uploader">${escapeHtml(item.uploader || '-')}</td>
    <td class="col-time">${formatTime(item.modified || item.created)}</td>
  `;

  tr.addEventListener('click', (e) => {
    if (e.target.type === 'checkbox') return;
    if (isFolderRow) {
      navigateRow(source, item, isUp);
      return;
    }
    source.selection.toggle(source.rowKey(item), !source.selection.has(key));
  });
  tr.querySelector('input[type="checkbox"]')?.addEventListener('change', (e) => {
    source.selection.toggle(source.rowKey(item), e.target.checked);
  });
  if (!isFolderRow) {
    tr.addEventListener('dblclick', () => openPreview(item, source.id));
    tr.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, source, item, {
        selection: source.selection,
        onAction: (cmdId, ctx) => runCommand(cmdId, ctx),
      });
    });
  } else if (isUp) {
    tr.addEventListener('contextmenu', (e) => e.preventDefault());
  } else if (source.id === 'netdisk') {
    tr.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showNetdiskFolderCtx(e.clientX, e.clientY, source, item);
    });
  } else {
    tr.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  return tr;
}

/** Netdisk folder context menu: open / rename / delete (recursive, strong confirm) / copy path. */
function showNetdiskFolderCtx(x, y, source, item) {
  const curDir = getState().netdiskPath || '/';
  const fullPath = `${curDir === '/' ? '' : curDir}/${item.name}`;
  showRaw(x, y, [
    { id: 'open', label: '打开', icon: 'FOLDER' },
    { id: 'rename', label: '重命名', icon: 'EDIT' },
    { id: 'sep', sep: true },
    { id: 'delete', label: '删除', icon: 'DELETE', danger: true },
    { id: 'copy-path', label: '复制路径', icon: 'COPY' },
  ], async (id) => {
    try {
      if (id === 'open') {
        set('netdiskPath', fullPath);
        set('netdiskPage', 1);
        refresh('netdisk');
      } else if (id === 'rename') {
        const name = await promptEx('重命名', `当前: ${item.name}`, { value: item.name });
        if (!name || name === item.name) return;
        await apiPost(API.BRIDGE.RENAME, { path: fullPath, name });
        toast('重命名成功', 'success');
        refresh('netdisk');
      } else if (id === 'delete') {
        const ok = await confirmEx('删除文件夹',
          `将删除「${item.name}」及其全部内容，不可恢复。`,
          { okText: '删除', danger: true });
        if (!ok) return;
        await apiPost(API.BRIDGE.REMOVE, { dir: curDir, names: [item.name] });
        toast('删除成功', 'success');
        refresh('netdisk');
      } else if (id === 'copy-path') {
        await copyToClipboard(fullPath);
        toast('路径已复制', 'success');
      }
    } catch (e) {
      toast(`操作失败: ${e.message || e}`, 'error');
    }
  });
}

/** Folder navigation: group folders are flat (single level), netdisk is a path stack. */
function navigateRow(source, item, isUp) {
  if (isUp) {
    if (source.id === 'group') {
      set('folder', '');
      set('folderChain', []);
    } else {
      const segs = (getState().netdiskPath || '/').split('/').filter(Boolean);
      set('netdiskPath', segs.length > 1 ? `/${segs.slice(0, -1).join('/')}` : '/');
    }
    set('filePage', 1);
    refresh(source.id === 'group' ? 'files' : 'netdisk');
    return;
  }
  if (source.id === 'group') {
    set('folderChain', [{ name: item.name }]);
    set('folder', item.name);
    set('filePage', 1);
    refresh('files');
  } else {
    const base = getState().netdiskPath === '/' ? '' : getState().netdiskPath;
    set('netdiskPath', `${base}/${item.name}`);
    set('netdiskPage', 1);
    refresh('netdisk');
  }
}

/** Standard empty-state text per source. */
export function emptyText(source) {
  // S1（2026-09-03）：聚合视图是设计而非警告——空态统一为「暂无文件」，
  // 不再常驻「未选择群：展示全部受管群文件」提示（聚合能力不变）。
  if (source.id === 'group') return '暂无文件';
  return '空目录';
}

/**
 * Keyed render into one or two panes (dual-pane layout splits the page,
 * single pane shows everything - N-07 rule 3 default).
 * @param {HTMLElement} container - the view's table host
 * @param {Object} source
 * @param {Array} items - file rows
 * @param {Array} folders - folder rows (group source)
 */
export function renderRows(container, source, items, folders = []) {
  const paneA = container.querySelector('.file-tbody[data-pane="a"]');
  const paneB = container.querySelector('.file-tbody[data-pane="b"]');
  if (!paneA) return;

  const dirRows = folders.map((f) => ({ ...f, is_folder: true, is_dir: true }));
  const inFolder = source.id === 'group'
    ? Boolean(getState().folder)
    : (getState().netdiskPath && getState().netdiskPath !== '/');
  const upRows = inFolder ? [{ name: '..', is_up: true, is_dir: true, is_folder: true, id: '..', size: 0 }] : [];
  const rows = [...upRows, ...dirRows, ...items.filter((f) => !f.is_dir)];

  if (rows.length === 0) {
    for (const tb of [paneA, paneB]) tb.innerHTML = '';
    // 2026-09-03 修复：空态行携带稳定 key（'empty'）——keyed diff 在其后数据到达时
    // 会把它纳入 toRemove 移除（此前无 key 的行永不释放，导致空态残留）。
    const tr = document.createElement('tr');
    tr.dataset.key = 'empty';
    tr.dataset.dir = '1';
    tr.innerHTML = `<td colspan="6" class="empty-hint">${emptyText(source)}</td>`;
    paneA.appendChild(tr);
    return;
  }

  if (getState().layout === 'single' || !paneB) {
    renderInto(paneA, source, rows);
    if (paneB) paneB.innerHTML = '';
    return;
  }
  const mid = Math.ceil(rows.length / 2);
  renderInto(paneA, source, rows.slice(0, mid));
  renderInto(paneB, source, rows.slice(mid));
}

function renderInto(tbody, source, rows) {
  if (rows.length === 0) {
    tbody.innerHTML = '';
    return;
  }
  applyKeyedDiff(tbody, rows, (item) => buildRow(source, item), (item) => rowKeyOf(source, item));
}

/** Reflect selection state onto rendered checkboxes without re-render (FE-13). */
export function updateCheckboxes(source) {
  document.querySelectorAll('tbody.file-tbody tr').forEach((tr) => {
    if (tr.dataset.dir === '1') return;
    const on = source.selection.has(tr.dataset.key);
    tr.classList.toggle('selected', on);
    const cb = tr.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = on;
  });
}

/** Header select-all reflects the current page (checked / indeterminate). */
export function syncSelectAll(source, scope = document) {
  const els = scope.querySelectorAll('.file-select-all');
  if (!els.length) return;
  const fileRows = (getState()[source.itemsKey] || []).filter((f) => !f.is_dir);
  const sel = fileRows.filter((f) => source.selection.has(source.rowKey(f))).length;
  for (const el of els) {
    el.checked = fileRows.length > 0 && sel === fileRows.length;
    el.indeterminate = sel > 0 && sel < fileRows.length;
  }
}

/** Update the pager strip from the loaded totals. */
export function updatePagination(container, source, prefix = 'file') {
  const st = getState();
  const page = st[source.pageKey] || 1;
  const pageSize = st.filePageSize || 24;
  const max = Math.ceil((st[source.totalKey] || 0) / pageSize) || 1;
  const info = container.querySelector(`#${prefix}-page-info`);
  if (info) info.textContent = `${page} / ${max} (共 ${st[source.totalKey] || 0} 项)`;
  const prev = container.querySelector(`#${prefix}-prev`);
  const next = container.querySelector(`#${prefix}-next`);
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= max;
}