/**
 * Group table data layer (E 组) - client-side full-list model.
 *
 * The groups endpoint returns the FULL list (no server pagination), so
 * filtering/sorting/paging happen here; rows render through the keyed
 * diff in slices per page to respect the render budget (FE-11/12). Group
 * roles are decoded via a display dictionary (FE-8).
 *
 * @module features/group-data
 */

import { getState, set } from '../store.js';
import { API, apiGet } from '../api.js';
import { escapeHtml, formatSize } from '../utils/helpers.js';
import { applyKeyedDiff } from '../utils/dom-diff.js';
import { navigate } from '../router.js';
import { toast } from '../components/toast.js';
import { showRaw } from '../components/context-menu.js';
import { handleMenuAction } from './group-actions.js';

const ROLE_LABELS = { owner: '群主', admin: '管理员', member: '成员' };

/** Filter (account) + sort + page slice of the full group list. */
export function groupSlice() {
  const { groups, groupPage, groupPageSize, accountFilter, groupSort } = getState();
  const list = accountFilter
    ? groups.filter((g) => (g.account_id || g.account || '') === accountFilter)
    : groups;
  const sorted = sortGroups(list, groupSort);
  // S4：pageSize=0 表示「全部」（一次显示全量）
  const size = groupPageSize > 0 ? groupPageSize : Math.max(sorted.length, 1);
  const start = (groupPage - 1) * size;
  return { slice: sorted.slice(start, start + size), total: sorted.length };
}

/** Fetch the full list (active or removed view) and render the page. */
export async function loadGroups(selectedGroups) {
  set('loading', true);
  try {
    const { accountFilter, groupSort, groupPage, groupPageSize, groupsView } = getState();
    const isRemoved = groupsView === 'removed';
    const data = isRemoved
      ? await apiGet(API.GROUPS.REMOVED, {})
      : await apiGet(API.GROUPS.LIST, {});
    const groups = data.groups || [];
    if (isRemoved) set('removedGroups', groups);
    else {
      set('groups', groups);
      renderGroupSelect(groups);
    }
    const filtered = filterByAccount(groups, accountFilter);
    const sorted = sortGroups(filtered, groupSort);
    const size = groupPageSize > 0 ? groupPageSize : Math.max(sorted.length, 1);
    const start = (groupPage - 1) * size;
    renderGroupRows(sorted.slice(start, start + size), selectedGroups, isRemoved);
    updateGroupInfo(filtered.length);
  } catch (e) {
    console.error('[groups] load failed:', e);
    toast('加载群列表失败', 'error');
  } finally {
    set('loading', false);
  }
}

/** Re-render the current page from state without refetching. */
export function rerenderGroups(selectedGroups) {
  const { groupsView, accountFilter, groupSort, groupPage, groupPageSize } = getState();
  const source = groupsView === 'removed' ? 'removedGroups' : 'groups';
  const full = getState()[source] || [];
  if (groupsView !== 'removed') renderGroupSelect(full);
  const filtered = filterByAccount(full, accountFilter);
  const sorted = sortGroups(filtered, groupSort);
  const size = groupPageSize > 0 ? groupPageSize : Math.max(sorted.length, 1);
  const start = (groupPage - 1) * size;
  renderGroupRows(sorted.slice(start, start + size), selectedGroups, groupsView === 'removed');
  updateGroupInfo(filtered.length);
}

export function filterByAccount(groups, accountFilter) {
  return accountFilter
    ? groups.filter((g) => (g.account_id || g.account || '') === accountFilter)
    : groups;
}

function renderGroupRows(groups, selectedGroups, isRemoved) {
  const paneA = document.querySelector('tbody.group-tbody[data-pane="a"]');
  const paneB = document.querySelector('tbody.group-tbody[data-pane="b"]');
  if (!paneA) return;

  if (groups.length === 0) {
    for (const tb of [paneA, paneB]) tb.innerHTML = '';
    // 2026-09-03 修复：空态行携带稳定 key（'empty'）——keyed diff 在其后有数据时移除
    const tr = document.createElement('tr');
    tr.dataset.key = 'empty';
    tr.dataset.gid = '';
    tr.innerHTML = `<td colspan="9" class="empty-hint">${isRemoved ? '没有已移除的群' : '暂无群数据'}</td>`;
    paneA.appendChild(tr);
    return;
  }

  if (getState().layout === 'single' || !paneB) {
    renderInto(paneA, groups, selectedGroups);
    if (paneB) paneB.innerHTML = '';
    return;
  }
  const mid = Math.ceil(groups.length / 2);
  renderInto(paneA, groups.slice(0, mid), selectedGroups);
  renderInto(paneB, groups.slice(mid), selectedGroups);
}

function renderInto(tbody, groups, selectedGroups) {
  if (groups.length === 0) {
    tbody.innerHTML = '';
    return;
  }
  applyKeyedDiff(tbody, groups, (g) => buildGroupRow(g, selectedGroups), (g) => g.group_id);
}

/** Build one group row: click sets the file context and opens the files tab. */
function buildGroupRow(g, selectedGroups) {
  const tr = document.createElement('tr');
  tr.dataset.gid = g.group_id;
  tr.dataset.key = g.group_id;
  tr.className = 'clickable';

  const selected = selectedGroups.has(g.group_id);
  tr.innerHTML = `
    <td class="col-chk"><input type="checkbox" ${selected ? 'checked' : ''} /></td>
    <td class="col-name"><span class="group-name">${escapeHtml(g.shown_name || g.group_name || g.group_id)}</span></td>
    <td class="col-id">${g.group_id}</td>
    <td class="col-label">${g.label ? `<span class="tag">${escapeHtml(g.label)}</span>` : ''}</td>
    <td class="col-role">${ROLE_LABELS[g.role] || g.role || '-'}</td>
    <td class="col-size"${g.total_space && g.total_space > 0
      ? ` title="总容量 ${formatSize(g.total_space)}${g.limit_count ? `· 文件数上限 ${g.limit_count}` : ''}"`
      : ''}>${g.total_space && g.total_space > 0
        ? `${formatSize(g.used_space)} / ${formatSize(g.total_space)}`
        : formatSize(g.used_space)}</td>
    <td class="col-album">${g.album_count || 0}</td>
    <td class="col-essence">${g.essence_count || 0}</td>
    <td class="col-scan">${g.last_scan_at || g.last_scan
      ? new Date((g.last_scan_at || g.last_scan) * 1000).toLocaleDateString('zh-CN')
      : '-'}</td>
  `;

  tr.addEventListener('click', (e) => {
    if (e.target.type === 'checkbox') return;
    set('currentGroup', g.group_id);
    set('filePage', 1);
    navigate('files');
  });
  tr.querySelector('input[type="checkbox"]')?.addEventListener('change', (e) => {
    if (e.target.checked) selectedGroups.add(g.group_id);
    else selectedGroups.delete(g.group_id);
    syncGroupSelectAll(selectedGroups);
  });
  tr.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    showGroupContextMenu(e.clientX, e.clientY, g, selectedGroups);
  });
  return tr;
}

/** Group-specific context menu: shows group actions at cursor position. */
function showGroupContextMenu(x, y, g, selectedGroups) {
  // Ensure the right-clicked group is in the selection so batch actions apply.
  if (!selectedGroups.has(g.group_id)) {
    selectedGroups.clear();
    selectedGroups.add(g.group_id);
    updateGroupCheckboxes(selectedGroups);
  }

  const items = [
    { id: 'sync', label: '群信息同步', icon: 'REFRESH' },
    { id: 'sort-label', label: '按编号排序', icon: 'MOVE' },
    { id: 'auto-label', label: '补编号', icon: 'EDIT' },
    { id: 'clear-labels', label: '清除编号', icon: 'X' },
    { id: 'sep', sep: true },
    { id: 'up', label: '上移选中', icon: 'ARROW_UP' },
    { id: 'down', label: '下移选中', icon: 'ARROW_DOWN' },
  ];
  showRaw(x, y, items, (act) => handleMenuAction(act, selectedGroups));
}

export function sortGroups(groups, sort) {
  const { key, dir } = sort;
  const mul = dir === 'asc' ? 1 : -1;
  return [...groups].sort((a, b) => {
    if (key === 'used_space') return ((a.used_space || 0) - (b.used_space || 0)) * mul;
    if (key === 'sort_order') return ((a.sort_order ?? 1e9) - (b.sort_order ?? 1e9)) * mul;
    const va = key === 'label' ? a.label : (a[key] ?? '');
    const vb = key === 'label' ? b.label : (b[key] ?? '');
    return String(va || '').localeCompare(String(vb || '')) * mul;
  });
}

export function updateGroupCheckboxes(selectedGroups) {
  document.querySelectorAll('tbody.group-tbody tr').forEach((tr) => {
    const cb = tr.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = selectedGroups.has(tr.dataset.gid);
  });
  syncGroupSelectAll(selectedGroups);
}

/** Header select-all reflects the current page selection. */
function syncGroupSelectAll(selectedGroups) {
  const els = document.querySelectorAll('.group-select-all');
  if (!els.length) return;
  const rows = document.querySelectorAll('tbody.group-tbody tr[data-key]');
  const sel = Array.from(rows).filter((tr) => selectedGroups.has(tr.dataset.key)).length;
  for (const el of els) {
    el.checked = rows.length > 0 && sel === rows.length;
    el.indeterminate = sel > 0 && sel < rows.length;
  }
}

export function updateGroupInfo(total) {
  const { groupPage, groupPageSize } = getState();
  const maxPage = groupPageSize > 0 ? (Math.ceil(total / groupPageSize) || 1) : 1;
  const info = document.getElementById('g-page-info');
  if (info) info.textContent = `${groupPage} / ${maxPage} (共 ${total} 个群)`;
  const count = document.getElementById('group-count');
  if (count) count.textContent = `${total} 个群`;
  const prev = document.getElementById('g-prev');
  const next = document.getElementById('g-next');
  if (prev) prev.disabled = groupPage <= 1;
  if (next) next.disabled = groupPage >= maxPage;
}

/** Accounts for the filter dropdown (live shape: [{account_id, groups, online}]). */
export async function loadAccounts() {
  try {
    const data = await apiGet(API.ACCOUNTS);
    const accounts = data.accounts || [];
    const ids = accounts.map((a) => (typeof a === 'string' ? a : (a.account_id || a)));
    set('accounts', ids);
    const sel = document.getElementById('account-filter');
    if (sel) {
      // Replace the options, never append a second copy on refresh.
      sel.replaceChildren();
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '全部账号';
      sel.appendChild(empty);
      ids.forEach((id) => {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = id;
        sel.appendChild(opt);
      });
    }
  } catch (e) { /* optional feature */ }
}

/** Group context select options, filtered by the account filter. */
export function renderGroupSelect(groups) {
  const sel = document.getElementById('file-group-select');
  if (!sel) return;
  const { accountFilter, currentGroup } = getState();
  const filtered = filterByAccount(groups, accountFilter);
  sel.innerHTML = '<option value="">全部群</option>' +
    filtered.map((g) =>
      `<option value="${escapeHtml(g.group_id)}"${g.group_id === currentGroup ? ' selected' : ''}>` +
      `${escapeHtml(g.shown_name || g.group_name || g.group_id)}（${g.group_id}）</option>`
    ).join('');
}