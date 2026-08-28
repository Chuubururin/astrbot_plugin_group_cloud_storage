/**
 * Group actions (E3-E11) - batch ops, remove/restore, ordering, labels.
 *
 * Extracted from the groups view to respect the <=300-line file rule.
 * All actions are OneBot11-API composites via the backend group endpoints.
 *
 * @module features/group-actions
 */

import { getState, set, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { confirmEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';

/**
 * Batch operations (S4, 2026-09-03 整改)：适配后端 action 契约
 * （groups/batch-ops {group_ids, action, value} 与 groups/batch items），
 * 覆盖：改名 / 加群方式 / 备注 / 编号 / 显示名。
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
    { name: 'value', label: '值', placeholder: '按所选操作填写（加群方式=1..5）' },
  ]);
  if (!res?.action) return;
  const gids = Array.from(selectedGroups);
  const action = res.action;

  try {
    if (action === 'add_option') {
      // 后端契约：add_option 为 1..5 整数（1=允许任何人，2=回答问题，3=成员邀请，4=管理员邀请，5=拒绝）
      const code = parseInt(String(res.value || '').trim(), 10);
      if (!code || code < 1 || code > 5) { toast('加群方式须为 1..5', 'warn'); return; }
      await apiPost(API.GROUPS.BATCH_OPS, { group_ids: gids, action: 'add_option', value: code });
    } else if (action === 'rename' || action === 'remark') {
      if (!res.value || !String(res.value).trim()) { toast(`请输入${action === 'rename' ? '群基础名' : '备注'}`, 'warn'); return; }
      await apiPost(API.GROUPS.BATCH_OPS, {
        group_ids: gids, action, value: String(res.value).trim(),
      });
    } else {
      // label / display_name：走 groups/batch items（批量标号/显示名）
      if (!res.value || !String(res.value).trim()) { toast('请输入值', 'warn'); return; }
      const body = action === 'label' ? 'label' : 'display_name';
      await apiPost(API.GROUPS.BATCH, {
        items: gids.map((gid) => ({ group_id: gid, [body]: String(res.value).trim() })),
      });
    }
    toast('批量操作任务已提交', 'success');
    refresh('groups');
  } catch (e) {
    toast(`批量操作失败: ${e.message || ''}`, 'error');
  }
}

/** Remove groups from management (listed in the removed view afterwards). */
export async function handleRemove(selectedGroups) {
  if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
  const ok = await confirmEx('移除管理', `确定移除 ${selectedGroups.size} 个群的管理？`, { danger: true, okText: '移除' });
  if (!ok) return;
  try {
    await apiPost(API.GROUPS.REMOVE, { group_ids: Array.from(selectedGroups) });
    toast('移除成功', 'success');
    selectedGroups.clear();
    refresh('groups');
  } catch (e) { toast('移除失败', 'error'); }
}

/** Restore removed groups back into management. */
export async function handleRestore(selectedGroups) {
  if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
  try {
    await apiPost(API.GROUPS.RESTORE, { group_ids: Array.from(selectedGroups) });
    toast('恢复成功', 'success');
    selectedGroups.clear();
    refresh('groups');
  } catch (e) { toast('恢复失败', 'error'); }
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

/** Menu actions: incremental sync, label sorting/autofill/clear, ordering. */
export async function handleMenuAction(act, selectedGroups) {
  switch (act) {
    case 'sync':
      try {
        await apiPost(API.GROUPS.SCAN, { mode: 'incremental' });
        toast('增量同步已启动', 'success');
      } catch (e) { toast('同步失败', 'error'); }
      break;
    case 'sort-label':
      set('groupSort', { key: 'label', dir: 'asc' });
      refresh('groups');
      break;
    case 'auto-label': {
      const ok = await confirmEx('补编号', '为所有群自动补编号？');
      if (!ok) return;
      try {
        await apiPost(API.GROUPS.BATCH, { action: 'auto_label' });
        toast('补编号完成', 'success');
        refresh('groups');
      } catch (e) { toast('补编号失败', 'error'); }
      break;
    }
    case 'clear-labels': {
      const ok = await confirmEx('清除编号', '清除所有群编号？', { danger: true });
      if (!ok) return;
      try {
        await apiPost(API.GROUPS.BATCH, { action: 'clear_labels' });
        toast('清除编号完成', 'success');
        refresh('groups');
      } catch (e) { toast('清除编号失败', 'error'); }
      break;
    }
    case 'up':
    case 'down': {
      if (selectedGroups.size === 0) { toast('请先选择群', 'warn'); return; }
      try {
        await apiPost(API.GROUPS.ORDER, {
          group_ids: Array.from(selectedGroups), direction: act,
        });
        toast(`${act === 'up' ? '上移' : '下移'}成功`, 'success');
        refresh('groups');
      } catch (e) { toast('移动失败', 'error'); }
      break;
    }
  }
}