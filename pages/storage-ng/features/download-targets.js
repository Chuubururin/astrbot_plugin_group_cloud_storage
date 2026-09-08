/**
 * Download/transfer target table - single source of truth for target ids
 * and their user-facing labels across every entry point (files toolbar,
 * cross-module distribute commands, context menus).
 *
 * Naming contract:
 *  - 下载到本地   = browser download (reassembly included for volumes)
 *  - 复制 CDN 直链 = QQ CDN original link (copy only)
 *  - 本机服务地址  = local download-service links (HTTP/SFTP/SMB)
 *  - 转存到 X     = server-side transfer into another storage medium
 *  - 复制全文     = essence text to clipboard (no transfer)
 *
 * @module features/download-targets
 */

/** Canonical target options (order = modal order). */
export const DOWNLOAD_TARGETS = [
  { value: 'local', label: '下载到本地（浏览器）' },
  { value: 'link', label: '复制 CDN 直链' },
  { value: 'address', label: '本机服务地址（HTTP/SFTP/SMB）' },
  { value: 'netdisk', label: '转存到网盘' },
  { value: 'album', label: '转存到相册' },
  { value: 'essence', label: '转存到精华' },
  { value: 'group', label: '转存到群文件' },
  { value: 'copy', label: '复制全文' },
];

/** Label for one target value ('' when unknown). */
export function targetLabel(value) {
  return (DOWNLOAD_TARGETS.find((t) => t.value === value) || {}).label || value;
}

/** Filter the canonical table to the allowed subset (keeps order). */
export function targetOptions(allowed) {
  return DOWNLOAD_TARGETS.filter((t) => !allowed || allowed.includes(t.value));
}
