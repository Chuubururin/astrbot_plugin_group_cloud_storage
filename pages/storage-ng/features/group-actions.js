/**
 * Group actions - batch ops, remove/restore, ordering, labels.
 *
 * Extracted from the groups view to respect the <=300-line file rule.
 * All actions are OneBot11-API composites via the backend group endpoints,
 * going through the shared mutate() write lifecycle.
 *
 * @module features/group-actions
 */

import { getState, set, refresh } from '../store.js';
import { API } from '../api.js';
import { confirmEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { mutate } from '../utils/mutate.js';
import { filterByAccount, sortGroups } from './group-data.js';

/**
 * Batch operations, adapted to the backend action contract
 * (groups/batch-ops {group_ids, action, value} and groups/batch items).
 * Covers: rename / join option / remark / label / display name.
 */
export async function handleBatchOps(selectedGroups) {
  if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
  const res = await showFormModal('批量操作', [
    { name: 'action', label: '操作', type: 'select', value: 'rename', options: [
      { value: 'rename', label: '批量改名（群基础名）' },
      { value: 'add_option', label: '批量加群方式' },
      { value: 'remark', label: '批量备注' },
      { value: 'label', label: '批量编号（本地标签）' },
      { value: 'display_name', label: '批量显示名' },
    ] },
    { name: 'value', label: '值', required: true, placeholder: '按所选操作填写（加群方式=1..5）' },
  ]);
  if (!res?.action) return;
  const gids = Array.from(selectedGroups);
  const action = res.action;

  let call = null;
  if (action === 'add_option') {
    // Backend contract: add_option is an integer 1..5
    // (1=anyone, 2=answer question, 3=member invite, 4=admin invite, 5=deny)
    const code = parseInt(String(res.value || '').trim(), 10);
    if (!code || code < 1 || code > 5) { toast('加群方式须为 1..5', 'warn'); return; }
    call = [API.GROUPS.BATCH_OPS, { group_ids: gids, action: 'add_option', value: code }];
  } else if (action === 'rename' || action === 'remark') {
    if (!res.value || !String(res.value).trim()) { toast(`请输入${action === 'rename' ? '群基础名' : '备注'}`, 'warn'); return; }
    call = [API.GROUPS.BATCH_OPS, { group_ids: gids, action, value: String(res.value).trim() }];
  } else {
    // label / display_name go through groups/batch items
    if (!res.value || !String(res.value).trim()) { toast('请输入值', 'warn'); return; }
    const body = action === 'label' ? 'label' : 'display_name';
    call = [API.GROUPS.BATCH, {
      items: gids.map((gid) => ({ group_id: gid, [body]: String(res.value).trim() })),
    }];
  }
  await mutate('批量操作', call[0], call[1], { refresh: 'groups', successText: '批量操作任务已提交' });
}

/** Remove groups from management (listed in the removed view afterwards). */
export async function handleRemove(selectedGroups) {
  if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
  const ok = await confirmEx('移除管理', `确定移除 ${selectedGroups.size} 个群的管理？`, { danger: true, okText: '移除' });
  if (!ok) return;
  const r = await mutate('移除', API.GROUPS.REMOVE,
    { group_ids: Array.from(selectedGroups) }, { refresh: 'groups' });
  if (r.ok) selectedGroups.clear();
}

/** Restore removed groups back into management. */
export async function handleRestore(selectedGroups) {
  if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
  await mutate('恢复', API.GROUPS.RESTORE,
    { group_ids: Array.from(selectedGroups) }, { refresh: 'groups' });
}

/** Toggle between the active list and the removed-groups view. */
export function handleToggleRemoved(selectedGroups) {
  const newView = getState().groupsView === 'active' ? 'removed' : 'active';
  set('groupsView', newView);
  set('groupPage', 1);
  selectedGroups.clear();
  const btn = document.getElementById('btn-removed');
  const restoreBtn = document.getElementById('btn-restore');
  if (btn) btn.textContent = newView === 'removed' ? '返回管理列表' : '已移除群';
  if (restoreBtn) restoreBtn.classList.toggle('hidden', newView !== 'removed');
  refresh('groups');
}

/**
 * Move the selected ids one step inside a visible order (block move: the
 * whole selection shifts by one position, keeping its relative order).
 * @param {string[]} order - id order, permuted in place
 * @param {string[]} ids - selected ids, pre-sorted in the move direction
 * @param {number} step - -1 (up) or 1 (down)
 * @returns {boolean} whether any exchange actually happened
 */
function swapNeighbours(order, ids, step) {
  let moved = false;
  for (const gid of ids) {
    const i = order.indexOf(gid);
    const j = i + step;
    if (i < 0 || j < 0 || j >= order.length) continue;
    [order[i], order[j]] = [order[j], order[i]];
    moved = true;
  }
  return moved;
}

/**
 * Write a permuted visible order back into the full order's own slots, so
 * the posted sequence still covers the whole table: groups hidden by the
 * account filter keep their position (groups/order is whole-table).
 * @param {string[]} full - full id order
 * @param {string[]} viewOrder - permuted visible id order (same ids)
 * @returns {string[]}
 */
function mergeVisibleOrder(full, viewOrder) {
  const ordered = full.slice();
  const visible = new Set(viewOrder);
  let slot = 0;
  full.forEach((gid, i) => { if (visible.has(gid)) ordered[i] = viewOrder[slot++]; });
  return ordered;
}

/** Menu actions: incremental sync, label sorting/autofill/clear, ordering. */
export async function handleMenuAction(act, selectedGroups) {
  switch (act) {
    case 'sync':
      await mutate('同步', API.GROUPS.SCAN, { mode: 'incremental' },
        { successText: '增量同步已启动' });
      break;
    case 'sort-label':
      set('groupSort', { key: 'label', dir: 'asc' });
      refresh('groups');
      break;
    case 'auto-label': {
      const ok = await confirmEx('补编号', '为所有群自动补编号？');
      if (!ok) return;
      await mutate('补编号', API.GROUPS.BATCH, { action: 'auto_label' },
        { refresh: 'groups', successText: '补编号完成' });
      break;
    }
    case 'clear-labels': {
      const ok = await confirmEx('清除编号', '清除所有群编号？', { danger: true });
      if (!ok) return;
      await mutate('清除编号', API.GROUPS.BATCH, { action: 'clear_labels' },
        { refresh: 'groups', successText: '清除编号完成' });
      break;
    }
    case 'up':
    case 'down': {
      if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
      // groups/order 只持久化受管群的整表顺序；已移除群不在该表内。
      if (getState().groupsView === 'removed') {
        toast('已移除群不支持排序', 'warn');
        return;
      }
      // 契约是整表位置持久化 {ordered_ids}: 全序恒取全量 groups,
      // accountFilter 只决定"相邻"是谁（可见顺序）。
      const all = getState().groups || [];
      const full = sortGroups(all, getState().groupSort).map((g) => g.group_id);
      const viewOrder = sortGroups(filterByAccount(all, getState().accountFilter || ''),
        getState().groupSort).map((g) => g.group_id);
      const step = act === 'up' ? -1 : 1;
      // 选择集是点击顺序而非位置顺序: 先按可见位置排序再逐个相邻交换
      // (上移自上而下、下移自下而上), 选中块整体挪一格且相对次序不变。
      const ids = Array.from(selectedGroups)
        .map((gid) => ({ gid, pos: viewOrder.indexOf(gid) }))
        .filter((e) => e.pos >= 0)
        .sort((a, b) => (step === -1 ? a.pos - b.pos : b.pos - a.pos))
        .map((e) => e.gid);
      // 无实际交换时不发请求、不报成功（静默无效的"上移成功"是缺陷）。
      if (!swapNeighbours(viewOrder, ids, step)) {
        toast(`${act === 'up' ? '上移' : '下移'}无效: 已到边界或无可见相邻群`, 'warn');
        return;
      }
      await mutate('移动', API.GROUPS.ORDER,
        { ordered_ids: mergeVisibleOrder(full, viewOrder) },
        { refresh: 'groups', successText: `${act === 'up' ? '上移' : '下移'}成功` });
      break;
    }
  }
}
