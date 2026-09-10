/**
 * Failure recovery - refetch the authoritative row state from the
 * corresponding cloud storage after an operation fails.
 *
 * User contract: when a triggered operation errors, the frontend must not
 * keep rendering a stale local copy of the entry. refetchRows() re-pulls
 * each affected row's original info from its storage medium:
 *  - files / albums / essence: files/detail (albums prefer albums/detail,
 *    which also self-heals a stale album_id)
 *  - netdisk: re-list the current bridge directory and re-match by path
 * Rows that no longer exist on the cloud are dropped from the table (they
 * cannot be acted on anymore); refreshed rows replace their stale copies
 * in place and re-render via the keyed diff.
 *
 * @module utils/recover
 */

import { API, apiGet, apiPost } from '../api.js';
import { getState, set } from '../store.js';
import { rowGroup } from './group.js';
import { toast } from '../components/toast.js';

/** List endpoint per source id ('' = unsupported -> no-op). */
const DETAIL_BY_SOURCE = {
  group: (state, row) =>
    apiGet(API.FILES.DETAIL, { id: Number(row.id), group: rowGroup(state, row) }),
  album: (state, row) => {
    const albumId = row.album_id || (row.meta && row.meta.album_id) || '';
    return albumId
      ? apiGet(API.ALBUMS.DETAIL, { album_id: albumId, name: row.name || '' })
      : apiGet(API.FILES.DETAIL, { id: Number(row.id), group: rowGroup(state, row), kind: 'album' });
  },
  essence: (state, row) =>
    apiGet(API.FILES.DETAIL, { id: Number(row.id), group: rowGroup(state, row), kind: 'essence' }),
};

/** Re-list the netdisk directory and return a path->row map.
 * Always page 1 with the max page size: the goal is a superset of the
 * visible rows, and continuing from the current page would skip them
 * (page numbers beyond the visible slice enumerate entries the table
 * never showed). An empty result is treated as "unknown", not as
 * "everything deleted" — a filter/pagination quirk must not wipe rows. */
async function netdiskRows() {
  try {
    const data = await apiPost(API.BRIDGE.NETDISK, {
      path: getState().netdiskPath || '/',
      page: 1,
      page_size: 500,
    });
    const items = data.items || [];
    if (!items.length) return null;
    return new Map(items.map((f) => [f.remote_path || f.name, f]));
  } catch {
    return null; // re-list failed too: leave rows untouched
  }
}

/**
 * Refetch rows for one source after a failure and patch the store.
 * @param {string} sourceId - 'group'|'album'|'essence'|'netdisk'
 * @param {Array<Object>} rows - the rows that belonged to the failed op
 * @returns {Promise<boolean>} true when any row was refreshed/removed
 */
export async function refetchRows(sourceId, rows) {
  const usable = (rows || []).filter((r) => r && !r.is_up && !r.is_dir);
  if (!usable.length) return false;
  const state = getState();
  let changed = false;

  if (sourceId === 'netdisk') {
    const fresh = await netdiskRows();
    if (!fresh) return false;
    const gone = usable.filter((r) => !fresh.has(r.remote_path || r.name));
    if (!gone.length) return false;
    // Deleted on the cloud -> drop from the table and repaint the tab.
    const goneKeys = new Set(gone.map((r) => r.remote_path || r.name));
    const cur = getState().netdiskFiles || [];
    set('netdiskFiles', cur.filter((r) => !goneKeys.has(r.remote_path || r.name)));
    const { refresh } = await import('../store.js');
    refresh('netdisk');
    return true;
  }

  const fetcher = DETAIL_BY_SOURCE[sourceId];
  if (!fetcher) return false;
  const itemsKey = sourceId === 'album' ? 'albumItems'
    : sourceId === 'essence' ? 'essenceItems' : 'fileItems';
  const gone = new Set(); // rows confirmed absent on the cloud (empty detail)
  const skipped = new Set(); // fetch errors: keep the stale row
  for (const row of usable) {
    try {
      const detail = await fetcher(state, row);
      if (!detail) {
        gone.add(String(row.id));
        continue;
      }
      // Patch the live list in place: replace the stale copy so the keyed
      // row diff re-renders it with fresh info on the next paint.
      const cur = getState()[itemsKey] || [];
      const idx = cur.findIndex((r) => String(r.id) === String(detail.id ?? row.id));
      if (idx >= 0) {
        const next = cur.slice();
        next[idx] = { ...cur[idx], ...detail };
        set(itemsKey, next);
        changed = true;
      }
    } catch {
      // 单行 detail 拉取失败（含 30s 超时/网络抖动）不能证明云端已删除：
      // 保留该行并保持陈旧数据，行消失的判定只信任明确的 404 语义。
      skipped.add(String(row.id));
    }
  }
  if (gone.size) changed = true;
  if (changed) {
    // Drop rows that no longer resolve, then repaint the tab.
    const cur = getState()[itemsKey] || [];
    set(itemsKey, cur.filter((r) => !gone.has(String(r.id))));
    const { refresh } = await import('../store.js');
    refresh(sourceId === 'album' ? 'albums' : sourceId === 'essence' ? 'essence' : 'files');
  }
  return changed;
}

/**
 * Recover after a failed command: toast + refetch. Silent no-op when the
 * caller opts out (cmd.recover === false) or nothing came back.
 */
export async function recoverAfterFailure(sourceId, ctx, message) {
  toast(message, 'error');
  try {
    const recovered = await refetchRows(sourceId, ctx.rows || []);
    if (recovered) toast('已从云端重新拉取该条目信息', 'info');
  } catch {
    // Recovery is best-effort; never mask the original failure.
  }
}
