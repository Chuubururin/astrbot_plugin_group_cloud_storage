/**
 * Ingest entries - every upload source form (N-06).
 *
 * Local uploads use the two-phase prepare/upload protocol from
 * features/upload.js. Cross-tab uploads (netdisk/album/essence -> files,
 * netdisk -> album, netdisk -> essence) delegate to features/cross-upload.js,
 * which drives the existing W2-A distribute endpoints.
 *
 * @module features/ingest
 */

import { getState, refresh } from '../store.js';
import { API, apiPost, upload as bridgeUpload } from '../api.js';
import { showFormModal, confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { resolveUploadGroup } from './upload.js';
import { handleNetdiskUploadLocal } from './netdisk-upload.js';
import {
  pickNetdiskFile, openNetdiskToGroup, openAlbumToGroup, openEssenceToGroup,
} from './cross-upload.js';

export { handleNetdiskUploadLocal };

const CONVERT_OPTIONS = [
  { value: '', label: '保持原格式' },
  { value: 'mp4', label: '转为 MP4' },
  { value: 'mkv', label: '转为 MKV' },
  { value: 'webm', label: '转为 WebM' },
  { value: 'png', label: '转为 PNG' },
  { value: 'jpg', label: '转为 JPG' },
  { value: 'webp', label: '转为 WebP' },
];

/** Options chosen in the album source modal, consumed by the hidden input. */
let albumUploadOptions = { lossy: false, convertTo: '' };

/**
 * Files-tab upload entry: one of the five owner-mandated sources.
 * Local opens the hidden picker; the other four execute directly.
 */
export async function showUploadSourceModal() {
  const res = await showFormModal('上传到群文件：选择来源', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: [
      { value: 'local', label: '本地文件' },
      { value: 'url', label: 'URL 链接' },
      // 2026-09-03 整改（S1）：原独立「URL 入库」按钮并入本模态（URL 链接），
      // 原「文本入库」按钮改为「从浏览器上传文本」并入本模态。
      { value: 'text', label: '从浏览器上传文本' },
      { value: 'netdisk', label: '由网盘上传' },
      { value: 'album', label: '由相册上传（图片/视频）' },
      { value: 'essence', label: '由精华上传（文本）' },
    ] },
  ]);
  if (!res?.source) return;
  if (res.source === 'local') {
    const fileInput = document.getElementById('file-input');
    if (fileInput) fileInput.click();
    return;
  }
  if (res.source === 'url') return openUrlIngest();
  if (res.source === 'text') return openTextIngest();
  if (res.source === 'netdisk') return openNetdiskToGroup();
  if (res.source === 'album') return openAlbumToGroup();
  if (res.source === 'essence') return openEssenceToGroup();
}

/** URL ingest form (files tab); optional W2-B conversion target. */
export async function openUrlIngest() {
  const { currentGroup } = getState();
  const group = currentGroup || await resolveUploadGroup('', 'file');
  if (!group) { toast('请先选择群', 'warn'); return; }
  const res = await showFormModal('URL 入库', [
    { name: 'url', label: 'URL', placeholder: 'https://...' },
    { name: 'filename', label: '文件名（可选）', placeholder: '自动检测' },
    { name: 'convert_to', label: '格式转换', type: 'select', value: '', options: CONVERT_OPTIONS },
  ]);
  if (!res?.url) return;
  try {
    await apiPost(API.FETCH, {
      group, url: res.url, name: res.filename || '', convert_to: res.convert_to || '',
    });
    toast('URL 入库任务已提交', 'success');
  } catch (e) { toast('URL 入库失败', 'error'); }
}

/** Text ingest form (files tab) - browser-input essence source. */
export async function openTextIngest() {
  const { currentGroup } = getState();
  const group = currentGroup || await resolveUploadGroup('', 'essence');
  if (!group) { toast('请先选择群', 'warn'); return; }
  const res = await showFormModal('文本入库', [
    { name: 'title', label: '标题', placeholder: '精华标题' },
    { name: 'text', label: '内容', type: 'textarea', rows: 8, placeholder: '输入文本内容...' },
  ]);
  if (!res?.text) return;
  try {
    // Server-side long-text sharding (L-3) is triggered automatically.
    await apiPost(API.ESSENCE.SAVE, { group, title: res.title || '', text: res.text });
    toast('文本入库成功', 'success');
  } catch (e) { toast('文本入库失败', 'error'); }
}

/**
 * Album upload entry (T-2): local media / URL image-video / netdisk media,
 * optional lossy warning and optional W2-B format conversion.
 * @param {HTMLElement} fileInput - hidden <input type=file accept=image/*,video/*>
 */
export async function showAlbumUploadModal(fileInput) {
  const res = await showFormModal('上传到相册', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: [
      { value: 'local', label: '本地图片/视频' },
      { value: 'url', label: 'URL 链接（图片/视频）' },
      { value: 'netdisk', label: '由网盘上传' },
    ] },
    { name: 'album', label: '相册名', value: 'AstrBotCloud', placeholder: '目标相册名' },
    { name: 'lossy', label: '媒体处理', type: 'select', value: 'auto', options: [
      { value: 'auto', label: '自动（长视频无损切割 <599s）' },
      { value: 'lossy', label: '有损压缩（重编码，不可逆）' },
    ] },
    { name: 'convert_to', label: '格式转换', type: 'select', value: '', options: CONVERT_OPTIONS },
  ]);
  if (!res?.source) return;
  albumUploadOptions = { lossy: res.lossy === 'lossy', convertTo: res.convert_to || '' };

  if (res.source === 'netdisk') {
    const file = await pickNetdiskFile();
    if (!file) return;
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    try {
      await apiPost(API.BRIDGE.NETDISK_DISTRIBUTE, {
        path: file.remote_path || file.name, target: 'album', group,
        name: file.name || '', lossy: albumUploadOptions.lossy || undefined,
        convert_to: albumUploadOptions.convertTo || '',
      });
      toast('网盘→相册转存已提交', 'success');
      refresh('albums');
    } catch (e) { toast(`转存失败: ${e.message || e}`, 'error'); }
    return;
  }

  if (res.source === 'url') {
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    const url = await showFormModal('URL 上传图片/视频', [
      { name: 'url', label: '媒体 URL', placeholder: 'https://...' },
    ]);
    if (!url?.url) return;
    try {
      await apiPost(API.FETCH, {
        group, url: url.url, to_album: true, album_name: res.album || 'AstrBotCloud',
        lossy: albumUploadOptions.lossy || undefined, convert_to: res.convert_to || '',
      });
      toast('相册上传任务已提交', 'success');
      refresh('albums');
    } catch (e) {
      toast(`相册上传失败: ${e.message || ''}`, 'error');
    }
    return;
  }

  if (albumUploadOptions.lossy) {
    const ok = await confirmEx('有损压缩',
      '媒体将经 ffmpeg 重编码上传（画质损失不可逆）。确定继续？');
    if (!ok) return;
  }
  if (fileInput) fileInput.click();
}

/**
 * Essence upload entry (T-3): browser input, document file, URL read, or a
 * netdisk document read. Text sharding is always server-side (L-3).
 */
export async function showEssenceUploadModal() {
  const res = await showFormModal('文本入库', [
    { name: 'source', label: '来源', type: 'select', value: 'input', options: [
      { value: 'input', label: '浏览器输入文本' },
      { value: 'file', label: '文档（文件）读取' },
      { value: 'url', label: 'URL 访问读取' },
      { value: 'netdisk', label: '网盘文档读取' },
    ] },
    { name: 'title', label: '标题', placeholder: '精华标题（可空）' },
    { name: 'text', label: '文本内容', type: 'textarea', rows: 8, placeholder: 'source=浏览器输入时填写' },
    { name: 'url', label: '文档 URL', placeholder: 'https://...' },
  ]);
  if (!res?.source) return;
  const group = await resolveUploadGroup(getState().essenceGroup, 'essence');
  if (!group) { toast('无可用目标群', 'warn'); return; }
  try {
    if (res.source === 'input') {
      if (!res.text) { toast('请输入文本内容', 'warn'); return; }
      await apiPost(API.ESSENCE.SAVE, { group, title: res.title || '', text: res.text });
    } else if (res.source === 'file') {
      const fileInput = document.getElementById('essence-file');
      if (fileInput) fileInput.click();
      toast('请选择文档文件（txt/md）——文本将分片入库精华', 'info');
      return;
    } else if (res.source === 'url') {
      if (!res.url) { toast('请输入文档 URL', 'warn'); return; }
      await apiPost(API.FETCH, { group, url: res.url, to_essence: true, name: res.title || '' });
    } else if (res.source === 'netdisk') {
      const file = await pickNetdiskFile();
      if (!file) return;
      await apiPost(API.BRIDGE.NETDISK_DISTRIBUTE, {
        path: file.remote_path || file.name, target: 'essence', group,
        name: file.name || '',
      });
    }
    toast('文本入库成功', 'success');
    refresh('essence');
  } catch (e) {
    toast(`Text ingest failed: ${e.message || ''}`, 'error');
  }
}

/**
 * Document file upload (essence tab): local docs through mode=text.
 * @param {FileList|File[]} files
 */
export async function handleEssenceFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;
  const group = await resolveUploadGroup(getState().essenceGroup, 'essence');
  if (!group) { toast('无可用目标群', 'warn'); return; }
  let ok = 0;
  for (const f of fileArr) {
    try {
      const prep = await apiPost(API.FILES.UPLOAD_PREPARE, {
        group, name: f.name, size: f.size, mode: 'text',
      });
      if (prep?.token) {
        await bridgeUpload(`${API.FILES.UPLOAD}/${prep.token}`, f);
        ok++;
      }
    } catch (e) {
      toast(`文档读取失败: ${f.name}`, 'error');
    }
  }
  if (ok > 0) {
    toast(`${ok}/${fileArr.length} 个文档已入库精华`, 'success');
    refresh('essence');
  }
}

/**
 * Album media upload (T-2): local images/videos via prepare/upload with
 * to_album=true (long videos auto-split server-side, L-2/C-2).
 * @param {FileList|File[]} files
 */
export async function handleAlbumFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;
  const group = await resolveUploadGroup(getState().albumGroup, 'album');
  if (!group) { toast('无可用目标群（请先在群组 Tab 加载群列表）', 'warn'); return; }
  let ok = 0;
  for (const f of fileArr) {
    try {
      // Client-side media type guess; the backend re-detects from bytes.
      const isVideo = /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i.test(f.name);
      const mode = isVideo ? 'video' : 'image';
      const convertOk = (isVideo && ['mp4', 'mkv', 'webm'].includes(albumUploadOptions.convertTo))
        || (!isVideo && ['png', 'jpg', 'jpeg', 'webp'].includes(albumUploadOptions.convertTo));
      const prep = await apiPost(API.FILES.UPLOAD_PREPARE, {
        group, name: f.name, size: f.size, mode, to_album: true,
        lossy: albumUploadOptions.lossy || undefined,
        convert_to: convertOk ? albumUploadOptions.convertTo : undefined,
      });
      if (prep?.token) {
        await bridgeUpload(`${API.FILES.UPLOAD}/${prep.token}`, f);
        ok++;
      }
    } catch (e) {
      toast(`相册上传失败: ${f.name}`, 'error');
    }
  }
  if (ok > 0) {
    toast(`${ok}/${fileArr.length} 个媒体上传成功`, 'success');
    refresh('albums');
  }
}
