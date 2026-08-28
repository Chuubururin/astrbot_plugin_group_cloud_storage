/**
 * API layer - the single gateway to the backend (FE-3).
 *
 * Every HTTP path lives in the API constant table below; building request
 * URLs by string concatenation anywhere else is a review reject. The
 * actual transport is the host-injected bridge SDK
 * (window.AstrBotPluginPage) - resolved lazily so the E2E harness can
 * substitute its fetch adapter before first use (TE-1).
 *
 * POST bodies (not query strings) carry parameters per the bridge contract.
 *
 * @module api
 */

/** Resolve the bridge SDK lazily (host injects it before scripts run). */
function sdk() {
  return window.AstrBotPluginPage;
}

/**
 * Backend endpoint paths, grouped by domain. Paths are relative to the
 * plugin extension root; the SDK prefixes the full route.
 */
export const API = {
  // ---- Groups (T-6) ----
  GROUPS: {
    LIST: 'groups',
    SCAN: 'groups/scan',
    BATCH: 'groups/batch',
    BATCH_OPS: 'groups/batch-ops',
    ORDER: 'groups/order',
    REMOVE: 'groups/remove',
    REMOVED: 'groups/removed',
    RESTORE: 'groups/restore',
  },

  // ---- Files (T-1) ----
  FILES: {
    LIST: 'files',                            // unified listing: files/albums/essence via kind
    DETAIL: 'files/detail',
    UPLOAD_PREPARE: 'files/upload/prepare',   // phase 1 of two-phase upload
    UPLOAD: 'files/upload',                   // phase 2 (append /<token>)
    DELETE: 'files/delete',
    BATCH_DELETE: 'files/batch-delete',
    MOVE: 'files/move',
    BATCH_MOVE: 'files/batch-move',
    REPLACE_NAME: 'files/replace_name',       // rename = re-upload (OneBot limit)
    CONVERT_VOLUMES: 'files/convert-volumes', // cloud big file -> volume set
    TAGS: 'files/tags',
    TAGCLOUD: 'files/tagcloud',
    BATCH_TAGS: 'files/batch-tags',
    LINKS: 'files/links',                     // batch direct links
    DOWNLOAD: 'files/download',
    LINK: 'files/link',
    ADDRESS: 'download/address',              // local download service (HTTP/FTP)
    URI: 'files/uri',
    SCAN: 'files/scan',
    SYNC: 'files/sync',
    VERIFY: 'files/verify',
    RECOMMEND_GROUP: 'files/recommend-group', // N-07 default upload target
    DISTRIBUTE: 'files/distribute',           // W2-A target distribution
    FOLDER_CREATE: 'files/folder-create',     // create group folder (flat single-level)
  },

  // ---- Albums (T-2) ----
  ALBUMS: {
    MEDIA: 'albums/media',
    VIDEO_PREVIEW: 'albums/video-preview',    // keyframe GIF generation
    DISTRIBUTE: 'albums/distribute',
  },

  // ---- Essence messages (T-3) ----
  ESSENCE: {
    SAVE: 'essence/save',
    TEXT: 'essence/text',
    DELETE: 'essence/delete',
    DISTRIBUTE: 'essence/distribute',
  },

  // ---- Netdisk / OpenList bridge (T-4) ----
  BRIDGE: {
    STATUS: 'bridge/status',
    TRANSFER: 'bridge/transfer',              // group files -> netdisk
    TRANSFER_IN: 'bridge/transfer-in',        // netdisk -> group files
    TASKS: 'bridge/tasks',
    NETDISK: 'bridge/netdisk',                // netdisk directory listing
    NETDISK_LINK: 'netdisk/link',
    NETDISK_INDEX: 'netdisk/index',
    NETDISK_UPLOAD_URL: 'netdisk/upload-url', // OpenList offline download
    NETDISK_DISTRIBUTE: 'netdisk/distribute',
    CANCEL: 'bridge/cancel',
    RETRY: 'bridge/retry',
    ARCHIVED: 'bridge/archived',
    CONFIG_GET: 'bridge/config/get',
    CONFIG_SAVE: 'bridge/config/save',
    MKDIR: 'netdisk/mkdir',
    RENAME: 'netdisk/rename',
    REMOVE: 'netdisk/remove',
    MOVE: 'netdisk/move',
    COPY: 'netdisk/copy',
    REMOVE_EMPTY_DIRS: 'netdisk/remove-empty-dirs',
    RECURSIVE_MOVE: 'netdisk/recursive-move',
    RENAME_BATCH: 'netdisk/rename-batch',
  },

  // ---- Task ledger (T-5): four actions + operation stream ----
  TASKS: 'tasks',
  TASKS_QUEUE: 'tasks/queue',
  TASKS_PAUSE: 'tasks/pause',
  TASKS_RESUME: 'tasks/resume',
  TASKS_RESUME_PENDING: 'tasks/resume-pending',
  TASKS_INTERRUPT: 'tasks/interrupt',
  TASKS_UNDO: 'tasks/undo',
  TASKS_OPS: 'tasks/ops',

  // ---- Config center (T-7) ----
  CONFIG_GET: 'config/get',
  CONFIG_SAVE: 'config/save',

  // ---- D-4 withering sync (17-spec endpoints) ----
  SYNC_WITHERING: 'sync/withering',
  SYNC_STATUS: 'sync/status',

  // ---- Cross-cutting ----
  EVENTS: 'events',              // SSE stream (sole queue-state channel)
  ACCOUNTS: 'accounts',          // multi-account registry
  STAT: 'stat',                  // aggregate stats
  FETCH: 'fetch',                // URL ingest pipeline
  PREVIEW_POLICY: 'preview/policy',
  META_CLASSIFY: 'meta/classify' // 13-class extension table (CT-9)
};

// ---- Frontend display dictionaries (FE-8: machine value -> label) ----

/** 13-class file classification (ADR-0008 N-01). */
export const TYPE_LABELS = {
  file: '文件',
  album: '相册',
  essence: '精华',
  document: '文稿',
  pdf: 'PDF',
  spreadsheet: '表格',
  slide: '幻灯片',
  online_doc: '在线文档',
  image: '图片',
  video: '视频',
  audio: '音频',
  archive: '压缩包',
  installer: '安装包',
  flash: '闪传文件',
  folder: '文件夹',
  other: '其他',
};

/** Derived storage-state filter (ADR-0008 N-02). */
export const STORE_STATUS_LABELS = {
  netdisk: '在网盘',
  album: '在相册',
  essence: '在精华消息',
  none: '未下载',
};

/** Bridge transfer task states. */
export const BRIDGE_STATE_LABELS = {
  pending: '等待中',
  running: '进行中',
  done: '已完成',
  failed: '失败',
};

/**
 * GET request.
 * @param {string} path - API path (key of API)
 * @param {Record<string, string>} [params] - query parameters
 * @returns {Promise<any>} decoded JSON body
 */
export async function apiGet(path, params) {
  return sdk().apiGet(path, params);
}

/**
 * POST request (body-carried parameters per bridge contract).
 * @param {string} path - API path
 * @param {any} [body] - request body
 * @returns {Promise<any>}
 */
export async function apiPost(path, body) {
  return sdk().apiPost(path, body || {});
}

/**
 * Upload a file (phase 2: append the prepare token to the path).
 * @param {string} path - upload path including /<token>
 * @param {File} file - file object
 * @param {function} [onProgress] - progress callback
 * @returns {Promise<any>}
 */
export async function upload(path, file, onProgress) {
  return sdk().upload(path, file, onProgress);
}

/**
 * Download via the bridge SDK (authenticated stream + filename hint).
 * @param {string} path - API path (e.g. API.FILES.DOWNLOAD)
 * @param {Record<string, any>} params - download parameters ({group, id})
 * @param {string} [filename] - suggested save name
 * @returns {Promise<void>}
 */
export async function download(path, params, filename) {
  return sdk().download(path, params, filename);
}

/**
 * Subscribe to SSE /events with a unified cancel-function contract.
 *
 * Host bridge (plugin_page_bridge.js, 2026-09-03 核对) expects a handlers
 * OBJECT ({onMessage, onError}) and returns a subscriptionId; message events
 * carry {raw, parsed}. The old code passed the raw handler function straight
 * through, so the host never invoked it (no heartbeats → watchdog always
 * degraded) and the returned promise was never unsubscribed (subscriptions
 * accumulated on every redial) — "连接断开，重连中" forever.
 *
 * E2E fetch adapter keeps a synchronous cancel-function contract and is
 * passed through unchanged.
 * @param {function(Object): void} handler - receives parsed event objects
 * @returns {function} cancel
 */
export function subscribeSSE(handler) {
  const bridge = sdk();
  if (!bridge || typeof bridge.subscribeSSE !== 'function') return () => {};

  let cancelled = false;
  const result = bridge.subscribeSSE('events', {
    onMessage(msg) {
      if (cancelled) return;
      const ev = msg && msg.parsed !== undefined ? msg.parsed : (msg ? msg.raw : msg);
      try { handler(ev); } catch (e) { console.error('[sse] handler error:', e); }
    },
    onError() {
      // Host-side channel error: emit nothing; the heartbeat watchdog
      // detects the dead stream and redials (utils/sse.js I5).
    },
  });

  // Async host bridge: Promise<subscriptionId>; cancel after it settles.
  if (result && typeof result.then === 'function') {
    let subId = null;
    result.then((id) => {
      subId = id == null ? null : String(id);
      if (cancelled && subId && typeof bridge.unsubscribeSSE === 'function') {
        try { bridge.unsubscribeSSE(subId); } catch (e) { /* already gone */ }
      }
    }).catch(() => { subId = null; });
    return () => {
      if (cancelled) return;
      cancelled = true;
      if (subId != null && typeof bridge.unsubscribeSSE === 'function') {
        try { bridge.unsubscribeSSE(subId); } catch (e) { /* already gone */ }
      }
    };
  }

  // Synchronous cancel-function contract (E2E fetch adapter).
  if (typeof result === 'function') return () => { try { result(); } catch (e) { /* already gone */ } };
  return () => {};
}

/**
 * Host page context (theme/platform), used for theme following (N-08).
 * @returns {{theme: string, platform: string}}
 */
export function getContext() {
  const b = sdk();
  return b && b.onContext ? b.onContext() : { theme: 'dark', platform: 'web' };
}
