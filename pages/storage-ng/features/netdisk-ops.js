/**
 * Netdisk operations - toolbar-level OpenList management actions.
 *
 * URL upload submits an OpenList offline download (zero local bytes) and
 * deep-index submits a recursive indexing task; both run as backend
 * tasks visible in the tasks tab and the bridge transfer panel.
 *
 * @module features/netdisk-ops
 */

import { getState, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { runEachWithFailures } from '../utils/helpers.js';
import { showFormModal, confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';

/** OpenList offline download form (zero local bytes). */
export async function openNetdiskUrlUpload() {
  const { netdiskPath } = getState();
  const res = await showFormModal('URL 上传到网盘', [
    { name: 'url', label: '文件 URL', required: true, placeholder: 'https://...' },
    { name: 'dir', label: '目标目录', value: netdiskPath || '/', placeholder: '/' },
  ]);
  if (!res?.url) return;
  try {
    const r = await apiPost(API.BRIDGE.NETDISK_UPLOAD_URL, { url: res.url, dir: res.dir || '/' });
    toast(`已提交离线下载${r?.task_id ? `（任务 ${String(r.task_id).slice(0, 8)}）` : ''}`, 'success');
    refresh('netdisk');
  } catch (e) { toast(`上传失败: ${e.message || ''}`, 'error'); }
}

/** Recursive netdisk indexing task (cancelable from the tasks tab). */

/**
 * Rename one netdisk entry by full path (shared by the command layer and the
 * folder context menu). Throws on failure.
 * @param {string} path
 * @param {string} name
 */
export async function netdiskRename(path, name) {
  return apiPost(API.BRIDGE.RENAME, { path, name });
}

/**
 * Remove netdisk entries given as full paths (splits dir/name per entry).
 * Collects per-item failures instead of stopping at the first.
 * Directory rows arrive with a trailing slash (remote_path convention);
 * strip it so the split yields the real name — an empty name would make
 * OpenList remove the parent directory instead.
 * @param {string[]} paths
 * @returns {Promise<{done: number, failed: string[]}>}
 */
export async function netdiskRemovePaths(paths) {
  return runEachWithFailures(paths, async (p) => {
    const clean = p.replace(/\/+$/, '') || '/';
    const dir = clean.replace(/\/[^/]+$/, '') || '/';
    await apiPost(API.BRIDGE.REMOVE, { dir, names: [clean.split('/').pop()] });
  });
}

