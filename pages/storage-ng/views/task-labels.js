/**
 * Display label tables shared across views (task kind/state names, badge
 * classes, 13-class type names, storage states, bridge states). Kind keys
 * must cover every string the backend submits through queue.submit() or
 * dispatches in op_dispatch; a missing key falls back to the raw kind.
 *
 * @module views/task-labels
 */

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

export const STATE_CLASS = {
  pending: 'st-pending',
  running: 'st-running',
  paused: 'st-paused',
  retry: 'st-running',
  done: 'st-done',
  failed: 'st-failed',
  cancelled: 'st-failed',
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

/** Bridge transfer task states. */
export const BRIDGE_STATE_LABELS = {
  pending: '等待中',
  running: '进行中',
  done: '已完成',
  failed: '失败',
};
