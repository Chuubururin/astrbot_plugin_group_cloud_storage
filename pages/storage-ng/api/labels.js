/** API labels — human-readable labels for task kinds and states. */

export const KIND_LABEL = {
  scan: '扫描',
  file_scan: '文件扫描',
  diff_file_scan: '差分对账',
  sync: '同步',
  upload: '上传',
  delete: '删除',
  rename: '改名',
  move: '移动',
  batch_groups: '批量群操作',
  bridge_out: '归档到网盘',
  bridge_in: '从网盘恢复',
  fetch: '外部导入',
  essence_save: '精华保存',
  essence_delete: '精华删除',
  video_upload: '视频上传',
  video_album: '视频相册',
  image_album: '图片相册',
  convert_volumes: '转分卷',
  create_folder: '创建目录',
  netdisk_index: '网盘索引',
  tags: '标签',
  replace_name: '改名重传',
  distribute: '下载分发',
};

export const STATE_LABEL = {
  pending: '排队中',
  running: '执行中',
  paused: '已暂停',
  retry: '重试中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

export function kindLabel(kind) {
  return KIND_LABEL[kind] || kind;
}

export function stateLabel(state) {
  return STATE_LABEL[state] || state;
}
