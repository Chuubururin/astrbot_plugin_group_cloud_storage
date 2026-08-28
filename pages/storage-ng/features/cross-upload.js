/**
 * Cross-upload relays - real resource pickers for cross-tab uploads.
 *
 * Netdisk, album and essence items are selected through a small picker and
 * moved with the existing W2-A distribute endpoints. No extra backend
 * route is created; this module only replaces the old "switch tab and read
 * a hint" workflow with the actual operation.
 *
 * @module features/cross-upload
 */

import { getState, refresh } from '../store.js';
import { API, apiGet, apiPost } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { resolveUploadGroup } from './upload.js';
import { rowGroup } from './commands.js';

/** Open a picker over the current netdisk listing (directories excluded). */
export async function pickNetdiskFile() {
  let items = getState().netdiskFiles || [];
  if (!items.length) {
    try {
      const data = await apiPost(API.BRIDGE.NETDISK, {
        path: getState().netdiskPath || '/', page: 1, page_size: 100,
      });
      items = data.items || data.files || [];
    } catch (e) {
      toast('网盘列表不可用', 'error');
      return null;
    }
  }
  const files = items.filter((f) => !f.is_dir && (f.remote_path || f.name));
  if (!files.length) { toast('当前网盘目录没有文件', 'warn'); return null; }
  const res = await showFormModal('选择网盘文件', [
    { name: 'idx', label: '文件', type: 'select', value: '0',
      options: files.map((f, i) => ({ value: String(i), label: f.remote_path || f.name })) },
  ]);
  if (!res) return null;
  return files[parseInt(res.idx, 10)] || null;
}

/** Pick one album/essence row from the unified resource directory. */
export async function pickCloudResource(kind) {
  const label = kind === 'album' ? '相册' : '精华';
  const group = getState().currentGroup
    || (kind === 'album' ? getState().albumGroup : getState().essenceGroup)
    || '';
  let data;
  try {
    data = await apiGet(API.FILES.LIST, {
      kind, group, page: 1, page_size: 100, sort: 'created_at', order: 'desc',
    });
  } catch (e) {
    toast(`${label} 列表不可用`, 'error');
    return null;
  }
  const items = data.items || [];
  if (!items.length) { toast(`没有可用的 ${label} 资源`, 'warn'); return null; }
  const res = await showFormModal(`选择 ${label} 资源`, [
    { name: 'idx', label: '资源', type: 'select', value: '0',
      options: items.map((f, i) => ({
        value: String(i),
        label: `${f.name || f.id}${f.group_name ? ` (${f.group_name})` : ''}`,
      })) },
  ]);
  if (!res) return null;
  return items[parseInt(res.idx, 10)] || null;
}

/** Cross-source: one netdisk file -> group files. */
export async function openNetdiskToGroup() {
  const file = await pickNetdiskFile();
  if (!file) return;
  const group = await resolveUploadGroup(getState().currentGroup, 'file');
  if (!group) { toast('无可用目标群', 'warn'); return; }
  try {
    await apiPost(API.BRIDGE.NETDISK_DISTRIBUTE, {
      path: file.remote_path || file.name, target: 'group', group, name: file.name || '',
    });
    toast('网盘→群文件转存已提交', 'success');
    refresh('files');
  } catch (e) { toast(`转存失败: ${e.message || e}`, 'error'); }
}

/** Cross-source: one album media -> group files. */
export async function openAlbumToGroup() {
  const row = await pickCloudResource('album');
  if (!row) return;
  const albumId = row.album_id || (row.meta && row.meta.album_id) || '';
  if (!albumId) { toast('所选相册缺少相册 ID', 'error'); return; }
  try {
    await apiPost(API.ALBUMS.DISTRIBUTE, {
      group: row.group_id || getState().albumGroup || '', album_id: albumId,
      name: row.name || '', target: 'group',
    });
    toast('相册→群文件转存已提交', 'success');
    refresh('files');
  } catch (e) { toast(`转存失败: ${e.message || e}`, 'error'); }
}

/** Cross-source: one essence text -> group files. */
export async function openEssenceToGroup() {
  const row = await pickCloudResource('essence');
  if (!row) return;
  try {
    await apiPost(API.ESSENCE.DISTRIBUTE, {
      group: row.group_id || getState().essenceGroup || '', id: Number(row.id), target: 'group',
    });
    toast('精华→群文件转存已提交', 'success');
    refresh('files');
  } catch (e) { toast(`转存失败: ${e.message || e}`, 'error'); }
}

/**
 * 2026-09-03 网盘上传矩阵直达（C 系列）：群文件/相册/精华 → 网盘。
 * 与 files 侧同款选择器，目标=netdisk（复用既有分发端点，零新端点）。
 */

/** 从群文件上传一个文件到网盘（群文件侧文件列表选择）。 */
export async function openGroupFileToNetdisk() {
  const group = getState().currentGroup || '';
  let data;
  try {
    data = await apiGet(API.FILES.LIST, {
      group, page: 1, page_size: 100, sort: 'created_at', order: 'desc',
    });
  } catch (e) {
    toast('群文件列表不可用', 'error');
    return;
  }
  const items = (data.items || []).filter((f) => !f.is_dir);
  if (!items.length) { toast('没有可用的群文件', 'warn'); return null; }
  const res = await showFormModal('选择群文件', [
    { name: 'idx', label: '文件', type: 'select', value: '0',
      options: items.map((f, i) => ({
        value: String(i), label: `${f.name} (${f.group_name || f.group_id})`,
      })) },
  ]);
  if (!res) return;
  const f = items[parseInt(res.idx, 10)];
  if (!f) return;
  try {
    await apiPost(API.FILES.DISTRIBUTE, {
      id: Number(f.id), group: rowGroup(getState(), f), target: 'netdisk',
    });
    toast('群文件→网盘任务已提交', 'success');
  } catch (e) { toast(`提交失败: ${e.message || ''}`, 'error'); }
}

/** 从相册上传（分发）一个媒体到网盘。 */
export async function openAlbumToNetdisk() {
  const row = await pickCloudResource('album');
  if (!row) return;
  const albumId = row.album_id || (row.meta && row.meta.album_id) || '';
  if (!albumId) { toast('所选相册缺少相册 ID', 'error'); return; }
  try {
    await apiPost(API.ALBUMS.DISTRIBUTE, {
      group: row.group_id || getState().albumGroup || '', album_id: albumId,
      name: row.name || '', target: 'netdisk',
    });
    toast('相册→网盘任务已提交', 'success');
  } catch (e) { toast(`提交失败: ${e.message || ''}`, 'error'); }
}

/** 从精华上传（分发）一条文本到网盘。 */
export async function openEssenceToNetdisk() {
  const row = await pickCloudResource('essence');
  if (!row) return;
  try {
    await apiPost(API.ESSENCE.DISTRIBUTE, {
      group: row.group_id || getState().essenceGroup || '', id: Number(row.id),
      target: 'netdisk',
    });
    toast('精华→网盘任务已提交', 'success');
  } catch (e) { toast(`提交失败: ${e.message || ''}`, 'error'); }
}
