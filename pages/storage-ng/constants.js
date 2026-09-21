/**
 * Constants - every magic number and machine-value enum .
 *
 * Backend constants keep the same names so the pairing is obvious.
 * Anything numeric or enumerable that appears in more than one place
 * must live here; literals inline in components are review rejects.
 *
 * @module constants
 */

// ---- Hard limits (platform constraints) ----
export const VOLUME_BYTES = 95 * 1024 * 1024;   // > this -> volume split (config volume_threshold '95MB')
export const QQ_TEXT_LIMIT = 4500;              // QQ single-message hard ceiling (not a split threshold)
export const ESSENCE_CHUNK_CHARS = 4000;        // essence split threshold (config essence_chunk_size)
export const VIDEO_SEGMENT_SECONDS = 599;       // album video: >= this must be split (config video_segment_seconds)
export const MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024;  // 2GB client upload cap (config fetch_max_size)
export const MAX_BATCH_ITEMS = 200;             // backend cap for files/batch-{delete,move,tags}
export const MAX_LINKS_ITEMS = 20;              // backend cap for files/links

// ---- Pagination ----
export const DEFAULT_PAGE_SIZE = 10;            // matches config page_size default; page may override
export const MAX_PAGE_SIZE = 100;

// ---- Timeouts and pacing ----
export const API_TIMEOUT = 30000;               // 30s API timeout
export const SEARCH_DEBOUNCE = 200;             // search input debounce (ms)
export const TOAST_DURATION = 3500;             // toast display time
export const QUEUE_POLL_INTERVAL = 2000;        // legacy queue poll fallback (2s)

// ---- SSE resilience (I5: heartbeat watchdog, not fixed polling) ----
export const SSE_HEARTBEAT_TIMEOUT_MS = 90000;  // 3 missed heartbeats -> forced redial
export const SSE_RECONNECT_BASE_MS = 1000;      // exponential backoff base
export const SSE_RECONNECT_MAX_MS = 30000;      // backoff cap

// ---- Render budget  ----
export const MAX_ROWS_PER_FRAME = 50;
export const TASK_LOG_LIMIT = 50;               // task panel log capacity

// ---- Marquee rectangle selection (C6) ----
export const MARQUEE_THRESHOLD_PX = 6;          // drag threshold vs plain click
export const MARQUEE_EDGE_PX = 60;              // viewport edge auto-scroll zone
export const MARQUEE_SCROLL_STEP = 14;
export const MARQUEE_CLICK_SUPPRESS_MS = 250;   // suppress row click after drag

// ---- Machine value enums  ----

/** SSE event types. */
export const EVENT_TYPES = {
  QUEUED: 'queued',
  STARTED: 'started',
  PROGRESS: 'progress',
  DONE: 'done',
  FAILED: 'failed',
  RETRY: 'retry',
  PAUSED: 'paused',
  RESUMED: 'resumed',
  CANCELLED: 'cancelled',
  DATA_CHANGED: 'data_changed',
  BRIDGE: 'bridge',              // bridge transfer lifecycle (per-task progress)
  HEARTBEAT: 'heartbeat',
};

/** Bridge transfer task states (machine values; labels in api.js). */
export const BRIDGE_STATES = {
  PENDING: 'pending',
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
  // Returned by the single-task query when the task id is unknown
  // (bridge/task) and written to archive_map by the inbound recovery path.
  UNKNOWN: 'unknown',
};

/** OpenList bridge capability states (bridge/status.capability). */
export const BRIDGE_CAPABILITIES = {
  DISABLED: 'disabled',
  UNKNOWN: 'UNKNOWN',
  OK: 'OK',
  BROKEN: 'BROKEN',
};

/** SSE event kinds that identify the producing subsystem. */
export const EVENT_KINDS = {
  SCAN: 'scan',
  FILE_SCAN: 'file_scan',
  DIFF_FILE_SCAN: 'diff_file_scan',
  SYNC: 'sync',
  UPLOAD: 'upload',
  DELETE: 'delete',
  MOVE_FILE: 'move_file',
  REPLACE_NAME: 'replace_name',
  CONVERT_VOLUMES: 'convert_volumes',
  CREATE_FOLDER: 'create_folder',
  BRIDGE_OUT: 'bridge_out',
  BRIDGE_IN: 'bridge_in',
  FETCH: 'fetch',
  ESSENCE_SAVE: 'essence_save',
  ESSENCE_DELETE: 'essence_delete',
  VIDEO_UPLOAD: 'video_upload',
  VIDEO_ALBUM: 'video_album',
  IMAGE_ALBUM: 'image_album',
  NETDISK_INDEX: 'netdisk_index',
  BATCH_GROUPS: 'batch_groups',
};

/**
 * data_changed kind -> store refresh topics (only data_changed
 * may reload a topic; the map keeps one refresh per topic).
 *
 * bridge_out/bridge_in never emit data_changed on the backend; their
 * DONE/FAILED/CANCELLED task events reuse this map for the affected
 * topics (netdisk for out, files for in) plus the bridge ledger.
 */
export const DATA_CHANGED_TOPICS = {
  scan: ['groups'],
  file_scan: ['files'],
  diff_file_scan: ['files', 'groups'],
  sync: ['files', 'groups'],
  upload: ['files'],
  delete: ['files'],
  move_file: ['files'],
  replace_name: ['files'],
  fetch: ['files'],
  essence_save: ['files', 'essence'],
  essence_delete: ['files', 'essence'],
  video_upload: ['files', 'albums'],
  video_album: ['files', 'albums'],
  image_album: ['files', 'albums'],
  convert_volumes: ['files'],
  create_folder: ['files'],
  rename: ['groups', 'files'],
  netdisk_index: ['netdisk'],
  batch_groups: ['groups'],
  bridge_out: ['netdisk', 'bridge'],
  bridge_in: ['files', 'bridge'],
};
