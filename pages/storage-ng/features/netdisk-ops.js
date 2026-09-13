/**
 * Netdisk operations - toolbar-level OpenList management actions.
 *
 * URL upload submits an OpenList offline download (zero local bytes) and
 * deep-index submits a recursive indexing task; both run as backend tasks
 * visible in the tasks tab and the bridge transfer panel. Move/copy group
 * full paths per source directory; remove-empty-dirs prunes empty direct
 * sub-directories of the current path.
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

/**
 * Group full paths by their parent directory, so one move/copy call covers
 * all siblings in that directory (the endpoint takes {src_dir, names}).
 * @param {string[]} paths
 * @returns {Map<string, string[]>} src_dir -> names
 */
function groupByDir(paths) {
  const groups = new Map();
  for (const p of paths) {
    const clean = p.replace(/\/+$/, '') || '/';
    const dir = clean.replace(/\/[^/]+$/, '') || '/';
    const name = clean.split('/').pop();
    if (!name) continue;
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(name);
  }
  return groups;
}

/**
 * Move entries (full paths) into dst_dir. One backend call per source
 * directory; collects per-group failures like netdiskRemovePaths.
 * OpenList fs/move is same-storage only: on failure the group falls back
 * to fs/recursive_move (cross-storage copy-then-delete, a server-side
 * task that finishes asynchronously).
 * @param {string[]} paths
 * @param {string} dstDir
 * @returns {Promise<{done: number, failed: string[], cross: number}>}
 */
export async function netdiskMovePaths(paths, dstDir) {
  const failed = [];
  let done = 0;
  let cross = 0;
  for (const [dir, names] of groupByDir(paths)) {
    try {
      await apiPost(API.BRIDGE.MOVE, { src_dir: dir, dst_dir: dstDir, names });
      done += names.length;
    } catch (e) {
      try {
        await apiPost(API.BRIDGE.RECURSIVE_MOVE, { src_dir: dir, dst_dir: dstDir, names });
        done += names.length;
        cross += 1;
      } catch (e2) {
        failed.push(`${dir}: ${e2.message || e2}`);
      }
    }
  }
  return { done, failed, cross };
}

/** Copy entries (full paths) into dst_dir; same grouping as move. */
export async function netdiskCopyPaths(paths, dstDir) {
  const failed = [];
  let done = 0;
  for (const [dir, names] of groupByDir(paths)) {
    try {
      await apiPost(API.BRIDGE.COPY, { src_dir: dir, dst_dir: dstDir, names });
      done += names.length;
    } catch (e) {
      failed.push(`${dir}: ${e.message || e}`);
    }
  }
  return { done, failed };
}

/** Deep-index a directory subtree (task; trackable in the tasks tab). */
export async function netdiskIndex(path) {
  return apiPost(API.BRIDGE.NETDISK_INDEX, { path });
}

/**
 * Remove empty direct sub-directories of src_dir (names come from the
 * currently loaded listing; OpenList refuses non-empty ones).
 */
export async function netdiskRemoveEmptyDirs(srcDir, names) {
  return apiPost(API.BRIDGE.REMOVE_EMPTY_DIRS, { src_dir: srcDir, names });
}

