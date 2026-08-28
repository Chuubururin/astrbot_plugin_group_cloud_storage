/**
 * Data sources - one abstract adapter per resource tab (FE-18).
 *
 * The resource table renders against a DataSource so a single
 * implementation serves group files, albums, essence and the netdisk.
 * Each adapter owns its page key and type-filter key, keeping the four
 * tabs fully isolated while still sharing one renderer.
 *
 * @module features/data-sources
 */

import { API, apiGet, apiPost } from '../api.js';
import { getState, selectionFor } from '../store.js';

/**
 * @typedef {Object} DataSource
 * @property {string} id - 'group'|'album'|'essence'|'netdisk'
 * @property {string} itemsKey - store key holding the current page items
 * @property {string} totalKey - store key holding the total count
 * @property {string} pageKey - store key holding the current page number
 * @property {string} typeKey - store key holding the local/server type filter
 * @property {Object} selection - per-module selection helper
 * @property {function(Object): string} rowKey - row key extractor
 * @property {boolean} serverSort - true when sort/filter is pushed to the server
 * @property {string[]} capabilities - action ids offered by the action bar
 * @property {function(Object, Object): Promise<Object>} list
 */

/** Group files: local index of the cloud truth (kind omitted = files). */
export const GROUP_SOURCE = {
  id: 'group',
  itemsKey: 'fileItems',
  totalKey: 'fileTotal',
  pageKey: 'filePage',
  selectedKey: 'fileSelected',
  typeKey: 'fileType',
  selection: selectionFor('fileSelected'),
  rowKey: (f) => String(f.id),
  serverSort: true,
  capabilities: [
    'download', 'link', 'address', 'bridge-out', 'move', 'rename',
    'volumes', 'tags', 'verify', 'detail', 'delete', 'clear', 'files-distribute',
  ],
  async list(state, params) {
    const query = {
      group: state.currentGroup,
      page: params.page,
      page_size: params.page_size,
    };
    // The folder class is a local row class, never sent as an ext filter.
    if (params.type && params.type !== 'folder') query.type = params.type;
    if (params.status) query.status = params.status;
    // Folder contract: '' = all, '__root__' = root only, else a flat folder name.
    if (params.folder) query.folder = params.folder;
    // #tag and filename search share the backend q channel (N-04).
    const q = state.tagFilter
      ? (params.q ? `${params.q} #${state.tagFilter}` : `#${state.tagFilter}`)
      : params.q;
    if (q) query.q = q;
    if (params.sort_by) {
      query.sort = params.sort_by;
      query.order = params.sort_dir;
    }
    const data = await apiGet(API.FILES.LIST, query);
    let items = data.items || data.files || [];
    if (params.type === 'folder') items = [];
    return {
      items,
      total: data.total || 0,
      folders: (data.folders || []).map((f) =>
        typeof f === 'string' ? { name: f } : f),
      tags: data.tags || null,
    };
  },
};

/** Albums: unified resource directory, kind=album, image/video only (T-2). */
export const ALBUM_SOURCE = {
  id: 'album',
  itemsKey: 'albumItems',
  totalKey: 'albumTotal',
  pageKey: 'albumPage',
  selectedKey: 'albumSelected',
  typeKey: '',
  selection: selectionFor('albumSelected'),
  rowKey: (f) => String(f.id),
  serverSort: true,
  capabilities: ['album-gallery', 'album-detail', 'album-distribute', 'clear'],
  async list(state, params) {
    const query = { kind: 'album', page: params.page, page_size: params.page_size };
    if (state.albumGroup) query.group = state.albumGroup;
    if (params.q) query.q = params.q;
    // Custom-tag filter rides the #tag search channel (module-isolated key).
    if (state.albumTagFilter) {
      query.q = query.q ? `${query.q} #${state.albumTagFilter}` : `#${state.albumTagFilter}`;
    }
    if (params.sort_by) { query.sort = params.sort_by; query.order = params.sort_dir; }
    const data = await apiGet(API.FILES.LIST, query);
    return { items: data.items || [], total: data.total || 0, folders: [], tags: data.tags || null };
  },
};

/** Essence messages: kind=essence, text only, full-text search (T-3). */
export const ESSENCE_SOURCE = {
  id: 'essence',
  itemsKey: 'essenceItems',
  totalKey: 'essenceTotal',
  pageKey: 'essencePage',
  selectedKey: 'essenceSelected',
  typeKey: '',
  selection: selectionFor('essenceSelected'),
  rowKey: (f) => String(f.id),
  serverSort: true,
  capabilities: ['essence-view', 'essence-detail', 'essence-delete', 'essence-distribute', 'clear'],
  async list(state, params) {
    const query = { kind: 'essence', page: params.page, page_size: params.page_size };
    if (state.essenceGroup) query.group = state.essenceGroup;
    if (params.q) query.q = params.q;
    if (state.essenceTagFilter) {
      query.q = query.q ? `${query.q} #${state.essenceTagFilter}` : `#${state.essenceTagFilter}`;
    }
    if (params.sort_by) { query.sort = params.sort_by; query.order = params.sort_dir; }
    const data = await apiGet(API.FILES.LIST, query);
    return { items: data.items || [], total: data.total || 0, folders: [], tags: data.tags || null };
  },
};

/** Netdisk: OpenList directory listing via the bridge (T-4). */
export const NETDISK_SOURCE = {
  id: 'netdisk',
  itemsKey: 'netdiskFiles',
  totalKey: 'netdiskTotal',
  pageKey: 'netdiskPage',
  selectedKey: 'netdiskSelected',
  typeKey: 'netdiskType',
  selection: selectionFor('netdiskSelected'),
  rowKey: (f) => f.remote_path || f.name,
  serverSort: false, // local sort/filter: the bridge list has no query params for them
  // 2026-09-03 整改（S2）：移除 transfer-in（与 netdisk-distribute target=group 重复）
  // 与 netdisk-index（深度索引无意义）。下载/直链/改名/标记/删除/分发/清空 保留。
  capabilities: [
    'netdisk-link', 'netdisk-download', 'netdisk-rename',
    'netdisk-tags', 'netdisk-delete', 'clear', 'netdisk-distribute',
  ],
  async list(state, params) {
    const data = await apiPost(API.BRIDGE.NETDISK, {
      path: state.netdiskPath || '/',
      page: params.page,
      page_size: params.page_size,
    });
    return {
      items: data.items || data.files || [],
      total: data.total || (data.items || data.files || []).length,
      folders: (data.items || []).filter((f) => f.is_dir),
      tags: null,
    };
  },
};

/**
 * 2026-09-03 网盘独立分类（不复用群文件 13 类）：
 * 文本 / 音频 / 视频 / 图片 / 其他——本地扩展名映射（不依赖服务器表）。
 */
const NETDISK_EXT_TYPES = {
  text: ['.txt', '.md', '.log', '.json', '.xml', '.yaml', '.yml', '.csv', '.ini', '.cfg', '.conf'],
  audio: ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus'],
  video: ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.wmv', '.ts', '.m4v'],
  image: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tif', '.tiff'],
};

/** 网盘扩展名 → 网盘分类（未匹配 → 其他） */
export function netdiskExtType(name) {
  const dot = String(name || '').lastIndexOf('.');
  const ext = dot > -1 ? String(name).slice(dot).toLowerCase() : '';
  for (const [type, exts] of Object.entries(NETDISK_EXT_TYPES)) {
    if (exts.includes(ext)) return type;
  }
  return 'other';
}

/** netdisk 过滤用映射（applyLocalFilterSort 兼容形态） */
export function netdiskTypeMap() {
  const map = new Map();
  for (const [type, exts] of Object.entries(NETDISK_EXT_TYPES)) {
    for (const e of exts) map.set(e, type);
  }
  return map;
}

/** Local type filter + sort for sources without server support (netdisk). */
export function applyLocalFilterSort(items, params, extTypes) {
  let rows = items;
  if (params.type) {
    rows = rows.filter((f) => {
      const name = f.name || '';
      const dot = name.lastIndexOf('.');
      const ext = dot > -1 ? name.slice(dot).toLowerCase() : '';
      const type = f.type || (extTypes && extTypes.get(ext)) || 'other';
      return type === params.type;
    });
  }
  const by = params.sort_by || 'name';
  const mul = params.sort_dir === 'desc' ? -1 : 1;
  return [...rows].sort((a, b) => {
    const va = a[by] ?? '';
    const vb = b[by] ?? '';
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * mul;
    return String(va).localeCompare(String(vb)) * mul;
  });
}

/** Resolve the DataSource of a tab id. */
export function sourceFor(view) {
  if (view === 'albums' || view === 'album') return ALBUM_SOURCE;
  if (view === 'essence') return ESSENCE_SOURCE;
  if (view === 'netdisk') return NETDISK_SOURCE;
  return GROUP_SOURCE;
}
