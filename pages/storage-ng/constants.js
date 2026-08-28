/**
 * Constants - every magic number and machine-value enum (FE-7).
 *
 * Backend constants keep the same names so the pairing is obvious.
 * Anything numeric or enumerable that appears in more than one place
 * must live here; literals inline in components are review rejects.
 *
 * @module constants
 */

// ---- Hard limits (owner constraints, ADR-0007 L series) ----
export const VOLUME_BYTES = 95 * 1024 * 1024;   // files above this split into volumes (95MB)
export const CHUNK_SIZE = 4500;                 // text chunk budget incl. reassembly marker
export const VIDEO_SEGMENT = 600;               // album video segment ceiling in seconds
export const MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024;  // 2GB client upload cap
export const MAX_BATCH_ITEMS = 20;              // per-request batch operation cap

// ---- Pagination ----
export const DEFAULT_PAGE_SIZE = 20;
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

// ---- Render budget (FE-16: max keyed-row writes per animation frame) ----
export const MAX_ROWS_PER_FRAME = 50;
export const TASK_LOG_LIMIT = 50;               // task panel log capacity

// ---- Marquee rectangle selection (C6) ----
export const MARQUEE_THRESHOLD_PX = 6;          // drag threshold vs plain click
export const MARQUEE_EDGE_PX = 60;              // viewport edge auto-scroll zone
export const MARQUEE_SCROLL_STEP = 14;
export const MARQUEE_CLICK_SUPPRESS_MS = 250;   // suppress row click after drag

// ---- Machine value enums (FE-8: backend emits, api.js maps to labels) ----

/** SSE event types (CT-6). */
export const EVENT_TYPES = {
  QUEUED: 'queued',
  STARTED: 'started',
  PROGRESS: 'progress',
  DONE: 'done',
  FAILED: 'failed',
  RETRY: 'retry',
  DATA_CHANGED: 'data_changed',
  HEARTBEAT: 'heartbeat',
};

/** Bridge transfer task states (machine values; labels in api.js). */
export const BRIDGE_STATES = {
  PENDING: 'pending',
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
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
  BRIDGE_OUT: 'bridge_out',
  BRIDGE_IN: 'bridge_in',
  FETCH: 'fetch',
  ESSENCE_SAVE: 'essence_save',
  ESSENCE_DELETE: 'essence_delete',
};

/**
 * data_changed kind -> store refresh topics (FE-14: only data_changed
 * may reload a topic; the map keeps one refresh per topic, FE-4).
 */
export const DATA_CHANGED_TOPICS = {
  scan: ['groups'],
  file_scan: ['files'],
  diff_file_scan: ['files', 'groups'],
  sync: ['groups'],
  upload: ['files'],
  delete: ['files'],
  move_file: ['files'],
  fetch: ['files'],
  essence_save: ['files', 'essence'],
  essence_delete: ['files', 'essence'],
  video_upload: ['files', 'albums'],
  video_album: ['files', 'albums'],
  convert_volumes: ['files'],
  batch_delete: ['files'],
  batch_tags: ['files'],
};
