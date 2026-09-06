/**
 * Preview routing + configurable preview policy .
 *
 * Built-in preview routes per resource type (file detail / album gallery /
 * essence text); the per-extension policy endpoint decides between
 * builtin, external (open a direct link, optionally through a template)
 * and download. Adjusting the policy table changes behavior without code
 * changes.
 *
 * @module features/preview
 */

import { getState } from '../store.js';
import { API, apiGet, apiPost, download } from '../api.js';
import { formatSize } from '../utils/helpers.js';
import { rowGroupFor } from '../utils/group.js';
import { detailEx } from '../components/modal.js';
import { showTextViewer } from '../components/text-viewer.js';
import { showGallery } from '../components/gallery.js';
import { toast } from '../components/toast.js';
import { normalizeMedia } from './album-media.js';

/** ext -> policy cache (backend default table + config overrides). */
const policyCache = new Map();

async function policyFor(name) {
  const dot = name.lastIndexOf('.');
  const ext = dot > -1 ? name.slice(dot).toLowerCase() : '';
  if (!ext) return { mode: 'builtin', type: 'other' };
  if (policyCache.has(ext)) return policyCache.get(ext);
  try {
    const p = await apiGet(API.PREVIEW_POLICY, { ext });
    policyCache.set(ext, p);
    return p;
  } catch (e) {
    return { mode: 'builtin', type: 'other' };
  }
}

async function previewEssence(row, sourceId) {
  try {
    // Row-first group: the essence view may list without a group filter.
    const group = rowGroupFor(getState(), row, sourceId || 'essence');
    const data = await apiGet(API.ESSENCE.TEXT, { id: row.id, group });
    showTextViewer(row.name, { text: data.text, missing: data.missing_parts || [] });
  } catch (e) {
    toast(`获取精华内容失败: ${e.message || e}`, 'error');
  }
}

async function previewAlbum(row, sourceId) {
  try {
    // The row carries its own group: the album view may list without a group
    // filter, so the module-scoped group state is not authoritative here.
    const group = rowGroupFor(getState(), row, sourceId || 'album');
    // Primary param album_id (QQ-assigned, alphanumeric); numeric row id
    // resolves server-side from local meta when the album ID is unknown.
    const data = await apiGet(API.ALBUMS.MEDIA, {
      album_id: row.album_id || '', id: row.id, group,
    });
    showGallery(row.name, normalizeMedia(data.media), {
      group,
      albumId: data.album_id || row.album_id || row.id || '',
      onChanged: () => refresh('albums'),
    });
  } catch (e) {
    toast(`获取相册失败: ${e.message || e}`, 'error');
  }
}

async function previewFileDetail(row, sourceId) {
  if (sourceId === 'netdisk') {
    await detailEx('网盘文件', [
      { label: '名称', value: row.name || '-' },
      { label: '路径', value: row.remote_path || '-' },
      { label: '大小', value: formatSize(row.size) },
      { label: '类型', value: row.type || '-' },
      { label: '标记', value: row.tags || '-' },
    ]);
    return;
  }
  const data = await apiGet(API.FILES.DETAIL, { id: row.id, group: rowGroupFor(getState(), row, sourceId) });
  await detailEx('文件详情', [
    { label: '名称', value: data.name || '-' },
    { label: '类型', value: data.type || '-' },
    { label: '大小', value: formatSize(data.size) },
    { label: '上传者', value: data.uploader || '-' },
    { label: '目录', value: data.folder || '/' },
    { label: '标签', value: (data.tags || []).join(', ') || '-' },
    { label: 'URI', value: data.uri || '-' },
  ]);
}

/**
 * Open the preview for a row; the policy endpoint picks the mode.
 * @param {{name: string, type?: string, id?: number, album_id?: string}} row
 * @param {string} [sourceId] - 'group'|'netdisk'|'album'|'essence'
 */
export async function openPreview(row, sourceId = 'group') {
  if (row.type === 'essence') return previewEssence(row, sourceId);
  if (row.type === 'album') return previewAlbum(row, sourceId);

  const policy = await policyFor(row.name || '');
  if (policy.mode === 'download') {
    if (sourceId === 'netdisk') {
      const d = await apiPost(API.BRIDGE.NETDISK_LINK, { path: row.remote_path || row.name });
      window.open(d.url || '', '_blank', 'noopener');
    } else {
      await download(API.FILES.DOWNLOAD, { id: row.id, group: rowGroupFor(getState(), row, sourceId) }, row.name);
      // 预览触发的下载也是用户操作：必须给出可见反馈，不留静默副作用
      toast(`已开始下载: ${row.name}`, 'success');
    }
    return;
  }
  if (policy.mode === 'external' && sourceId !== 'netdisk') {
    try {
      const d = await apiGet(API.FILES.LINK, { id: row.id, group: rowGroupFor(getState(), row, sourceId) });
      const url = (d && (d.url || d.link)) || '';
      if (!url) { toast('直链获取失败', 'error'); return; }
      const target = policy.template
        ? policy.template.replace('{src}', encodeURIComponent(url))
        : url;
      window.open(target, '_blank', 'noopener');
      return;
    } catch (e) { /* fall through to builtin */ }
  }
  return previewFileDetail(row, sourceId);
}