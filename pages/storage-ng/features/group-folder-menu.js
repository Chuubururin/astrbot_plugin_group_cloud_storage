/**
 * Group folder context menu — rename / delete / copy name for folder rows
 * in the group-files view. Wires the files/folder-rename and
 * files/folder-delete endpoints; folder rows could only navigate before.
 *
 * @module features/group-folder-menu
 */

import { getState, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { copyToClipboard } from '../utils/helpers.js';
import { rowGroupFor } from '../utils/group.js';
import { showRaw } from '../components/context-menu.js';
import { promptEx, confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';

/** Build and show the folder-row context menu (group files source only). */
export function showGroupFolderCtx(x, y, item) {
  // The ".." row is navigation, not a folder. file-rows.js already keeps it
  // out of this branch, but the guard is repeated here because folder-delete
  // is destructive and a stray caller must not be able to reach it.
  if (item && (item.is_up || String(item.id || '') === '..')) return;
  // Row-first: the aggregated view has no currentGroup, but every folder
  // row carries its own group_id, so the row is authoritative.
  const groupId = rowGroupFor(getState(), item, 'group');
  const folderId = String(item.id || '');
  showRaw(x, y, [
    { id: 'rename', label: '重命名', icon: 'EDIT' },
    { id: 'sep', sep: true },
    { id: 'delete', label: '删除', icon: 'DELETE', danger: true },
    { id: 'copy-path', label: '复制名称', icon: 'COPY' },
  ], async (id) => {
    try {
      if (!groupId || !folderId) { toast('无法定位文件夹', 'error'); return; }
      if (id === 'rename') {
        const name = await promptEx('重命名文件夹', `当前: ${item.name}`, { value: item.name });
        if (!name || name === item.name) return;
        await apiPost(API.FILES.FOLDER_RENAME, { group: groupId, folder_id: folderId, name });
        toast('重命名成功', 'success');
        refresh('files');
      } else if (id === 'delete') {
        const ok = await confirmEx('删除文件夹',
          `将删除群 ${groupId} 中的「${item.name}」及其全部内容，不可恢复。`,
          { okText: '删除', danger: true });
        if (!ok) return;
        await apiPost(API.FILES.FOLDER_DELETE, { group: groupId, folder_id: folderId });
        toast('文件夹已删除', 'success');
        refresh('files');
      } else if (id === 'copy-path') {
        const ok = await copyToClipboard(item.name || '');
        toast(ok ? '名称已复制' : '复制失败，请手动复制', ok ? 'success' : 'error');
      }
    } catch (e) {
      toast(`操作失败: ${e.message || e}`, 'error');
    }
  });
}
