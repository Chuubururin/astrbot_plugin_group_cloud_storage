/**
 * Display label tables shared across views (task kind/state names, badge
 * classes, 13-class type names, storage states, bridge states), plus the task
 * ledger's summary text and its keyed-diff render signature. Kind keys
 * must cover every string the backend submits through queue.submit() or
 * dispatches in op_dispatch; a missing key falls back to the raw kind.
 *
 * @module views/task-labels
 */

import { BRIDGE_STATES, BRIDGE_CAPABILITIES } from '../constants.js';

/** Undoable operation kinds (aligned with task_control._REVERSIBLE_KINDS). */
export const REVERSIBLE_KINDS = new Set(['move_file', 'replace_name', 'tags']);

export const STATE_FILTERS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'retry', label: '重试中' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

export const STATE_LABEL = {
  pending: '排队中',
  running: '运行中',
  paused: '已暂停',
  retry: '重试中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

/**
 * State -> badge/row colour class. Covers both vocabularies that reach the
 * UI: the op_ledger states (pending/running/paused/retry/done/failed/
 * cancelled) and the SSE event types the task panel logs (queued/started/
 * progress/resumed) - a missing key renders the row with no state colour.
 */
export const STATE_CLASS = {
  pending: 'st-pending',
  running: 'st-running',
  paused: 'st-paused',
  retry: 'st-running',
  done: 'st-done',
  failed: 'st-failed',
  cancelled: 'st-failed',
  queued: 'st-pending',
  started: 'st-running',
  progress: 'st-running',
  resumed: 'st-running',
};

export const KIND_LABEL = {
  move_file: '移动文件',
  replace_name: '改名',
  delete: '删除',
  file_scan: '文件扫描',
  diff_file_scan: '差分扫描',
  convert_volumes: '转分卷',
  video_upload: '视频上传',
  video_album: '视频相册',
  image_album: '图片相册',
  fetch: '抓取',
  essence_save: '精华保存',
  essence_delete: '精华删除',
  netdisk_index: '网盘索引',
  tags: '标签',
  upload: '上传',
  create_folder: '创建目录',
  bridge_out: '归档到网盘',
  bridge_in: '从网盘恢复',
  batch_groups: '批量群操作',
  scan: '群扫描',
  sync: '同步',
  sync_all: '全量同步',
  rename: '改名',
};

// ---- Cross-view display dictionaries ----

/** 13-class file classification. */
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

/** Derived storage-state filter. */
export const STORE_STATUS_LABELS = {
  netdisk: '在网盘',
  album: '在相册',
  essence: '在精华消息',
  none: '未下载',
};

/**
 * Bridge transfer task states (archive_map.state). Keys are the machine
 * values from constants.BRIDGE_STATES - including `unknown`, which the
 * single-task query returns for an unrecognised task id.
 */
export const BRIDGE_STATE_LABELS = {
  [BRIDGE_STATES.PENDING]: '等待中',
  [BRIDGE_STATES.RUNNING]: '进行中',
  [BRIDGE_STATES.DONE]: '已完成',
  [BRIDGE_STATES.FAILED]: '失败',
  [BRIDGE_STATES.UNKNOWN]: '未知',
};

/**
 * OpenList bridge capability states (bridge/status.capability). Callers must
 * fall back to 未知 rather than printing the raw value.
 */
export const BRIDGE_CAPABILITY_LABELS = {
  [BRIDGE_CAPABILITIES.DISABLED]: '未启用',
  [BRIDGE_CAPABILITIES.UNKNOWN]: '未知',
  [BRIDGE_CAPABILITIES.OK]: '正常',
  [BRIDGE_CAPABILITIES.BROKEN]: '异常',
};

/** 仅以 name/url 作为摘要的 kind。 */
const NAME_ONLY_KINDS = new Set(['convert_volumes', 'video_upload', 'video_album', 'image_album', 'fetch']);

/** Build a human-readable detail summary from a task. */
export function taskSummary(t) {
  const p = t.payload || {}, k = t.kind || '';
  if (k === 'move_file' || k === 'replace_name') {
    const move = k === 'move_file';
    const from = move ? (p.from_folder || p.old_folder || '') : (p.old_name || p.name || '');
    const to = move ? (p.to_folder || p.folder || '') : p.new_name;
    if (from || to) return `${from || '?'} → ${to || '?'}${move && p.name ? ` (${p.name})` : ''}`;
  }
  if (k === 'delete') return (p.name || p.ids?.length) ? `${p.ids?.length || 1} 个文件` : '';
  if (k === 'tags') return `标签: ${(p.tags || p.after?.tags || []).join(', ') || '-'}`;
  if (k === 'file_scan' || k === 'diff_file_scan') {
    const g = p.groups || [];
    return g.length ? `${g.length} 个群` : '全群扫描';
  }
  if (k === 'essence_save') return p.title || '';
  if (k === 'netdisk_index') return p.path || '';
  return NAME_ONLY_KINDS.has(k) ? (p.name || p.url || '') : '';
}

/**
 * Projection of exactly the fields the task ledger row renders.
 *
 * taskSummary already collapses every payload variant into the string the cell
 * shows, so the row no longer needs a deep compare of `payload` - the largest
 * source of false-positive row rewrites in this table.
 * Keep in sync with views/tasks.js buildTaskRow / actionCell.
 * @param {Object} t
 * @returns {string}
 */
export function taskSignature(t) {
  return [t.kind, t.target, t.state, t.error, t.created_at, taskSummary(t)].join('\u0000');
}
