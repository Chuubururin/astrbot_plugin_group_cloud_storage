/**
 * Ingest entries - every upload source form .
 *
 * Local uploads use the two-phase prepare/upload protocol from
 * features/upload.js. Cross-tab uploads (netdisk/album/essence -> files,
 * netdisk -> album, netdisk -> essence) delegate to features/cross-upload.js,
 * which drives the existing distribute endpoints.
 *
 * @module features/ingest
 */

import { getState, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { showFormModal, confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { mutate } from '../utils/mutate.js';
import { resolveUploadGroup, uploadOnce } from './upload.js';
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
let albumUploadOptions = { convertTo: '' };

/**
 * Files-tab upload entry: one of the five owner-mandated sources.
 * Local opens the hidden picker; the other four execute directly.
 */
export async function showUploadSourceModal() {
  const res = await showFormModal('上传到群文件：选择来源', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: [
      { value: 'local', label: '本地文件' },
      { value: 'url', label: 'URL 链接' },
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

/** URL ingest form (files tab); optional format conversion target. */
export async function openUrlIngest() {
  const { currentGroup } = getState();
  const group = currentGroup || await resolveUploadGroup('', 'file');
  if (!group) { toast('请先选择群', 'warn'); return; }
  const res = await showFormModal('URL 导入', [
    { name: 'url', label: 'URL', required: true, placeholder: 'https://...' },
    { name: 'filename', label: '文件名（可选）', placeholder: '自动检测' },
    { name: 'convert_to', label: '格式转换', type: 'select', value: '', options: CONVERT_OPTIONS },
  ]);
  if (!res?.url) return;
  await mutate('URL 导入', API.FETCH, {
    group, url: res.url, name: res.filename || '', convert_to: res.convert_to || '',
  }, { successText: 'URL 导入任务已提交' });
}

/** Text ingest form (files tab) - browser-input essence source. */
export async function openTextIngest() {
  const { currentGroup } = getState();
  const group = currentGroup || await resolveUploadGroup('', 'essence');
  if (!group) { toast('请先选择群', 'warn'); return; }
  const res = await showFormModal('文本保存为精华', [
    { name: 'title', label: '标题（可选）', placeholder: '精华标题' },
    { name: 'text', label: '内容', type: 'textarea', rows: 8, required: true, placeholder: '输入文本内容...' },
  ]);
  if (!res?.text) return;
  // Server-side long-text sharding  is triggered automatically.
  await mutate('保存', API.ESSENCE.SAVE, { group, title: res.title || '', text: res.text });
}

/**
 * Album upload entry : local media / URL image-video / netdisk media.
 * Lossy re-encode is the user's per-upload choice (checkbox + quality
 * tier, irreversible); optional format conversion remains.
 * @param {HTMLElement} fileInput - hidden <input type=file accept=image/*,video/*>
 */
export async function showAlbumUploadModal(fileInput) {
  const res = await showFormModal('上传到相册', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: [
      { value: 'local', label: '本地图片（视频上传施工中，暂未实现）' },
      { value: 'url', label: 'URL 链接（图片；视频施工中）' },
      { value: 'netdisk', label: '由网盘上传（图片；视频施工中）' },
    ] },
    { name: 'album', label: '相册名', value: 'AstrBotCloud', placeholder: '目标相册名' },
    { name: 'convert_to', label: '格式转换', type: 'select', value: '', options: CONVERT_OPTIONS },
    { name: 'lossy', label: '有损压缩（重编码，不可逆）', type: 'select', value: '', options: [
      { value: '', label: '不压缩（原图上传）' },
      { value: 'high', label: '轻度（画质优先）' },
      { value: 'medium', label: '均衡' },
      { value: 'low', label: '强力（体积优先）' },
    ] },
  ]);
  if (res?.source) {
    albumUploadOptions = {
      convertTo: res.convert_to || '',
      lossy: res.lossy || '',
      albumName: res.album || 'AstrBotCloud',
    };
  }

  if (res.source === 'netdisk') {
    const file = await pickNetdiskFile();
    if (!file) return;
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    await mutate('转存', API.BRIDGE.NETDISK_DISTRIBUTE, {
      path: file.remote_path || file.name, target: 'album', group,
      name: file.name || '',
      convert_to: albumUploadOptions.convertTo || '',
    }, { refresh: 'albums', successText: '网盘→相册转存已提交' });
    return;
  }

  if (res.source === 'url') {
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    const url = await showFormModal('URL 上传图片/视频', [
      { name: 'url', label: '媒体 URL', required: true, placeholder: 'https://...' },
    ]);
    if (!url?.url) return;
    await mutate('相册上传', API.FETCH, {
      group, url: url.url, to_album: true, album_name: albumUploadOptions.albumName || 'AstrBotCloud',
      convert_to: res.convert_to || '',
      lossy: Boolean(albumUploadOptions.lossy),
      lossy_level: albumUploadOptions.lossy || undefined,
    }, { refresh: 'albums', successText: '相册上传任务已提交' });
    return;
  }

  if (fileInput) fileInput.click();
}

/**
 * Essence upload entry : browser input, document file, URL read, or a
 * netdisk document read. Text sharding is always server-side .
 */
export async function showEssenceUploadModal() {
  const res = await showFormModal('文本保存为精华', [
    { name: 'source', label: '来源', type: 'select', value: 'input', options: [
      { value: 'input', label: '浏览器输入文本' },
      { value: 'file', label: '文档（文件）读取' },
      { value: 'url', label: 'URL 访问读取' },
      { value: 'netdisk', label: '网盘文档读取' },
    ] },
    { name: 'title', label: '标题（可选）', placeholder: '精华标题（可空）' },
    { name: 'text', label: '文本内容（来源=浏览器输入时必填）', type: 'textarea', rows: 8, placeholder: 'source=浏览器输入时填写' },
    { name: 'url', label: '文档 URL（来源=URL 访问读取时必填）', placeholder: 'https://...' },
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
      toast('请选择文档文件（txt/md）——文本将分段保存为精华消息', 'info');
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
    toast('保存成功', 'success');
    refresh('essence');
  } catch (e) {
    toast(`保存失败: ${e.message || ''}`, 'error');
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
  const failed = [];
  for (const f of fileArr) {
    try {
      const r = await uploadOnce(group, { file: f }, { mode: 'text' });
      if (r.ok) ok++;
      else failed.push(`${f.name}: ${r.error || 'prepare 被拒绝'}`);
    } catch (e) {
      failed.push(`${f.name}: ${e.message || e}`);
    }
  }
  if (failed.length) {
    toast(`${ok}/${fileArr.length} 个文档已保存；失败: ${failed.join('；')}`, 'warn');
  } else if (ok > 0) {
    toast(`${ok}/${fileArr.length} 个文档已保存为精华消息`, 'success');
  }
  if (ok > 0) refresh('essence');
}

/**
 * Album media upload : local images/videos via prepare/upload with
 * to_album=true .
 * @param {FileList|File[]} files
 */
export async function handleAlbumFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;
  // Album video upload: the protocol side does not support it yet (the
  // framework hook is reserved until then).
  const isVideoName = (n) => /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i.test(n);
  const videos = fileArr.filter((f) => isVideoName(f.name));
  const images = fileArr.filter((f) => !isVideoName(f.name));
  if (videos.length > 0) {
    if (images.length === 0) {
      toast('相册视频上传施工中，暂未实现（协议端限制；框架已保留）', 'warn');
      return;
    }
    const ok = await confirmEx(
      '相册视频上传：施工中',
      `协议端暂不支持向群相册上传视频（框架已保留）。已跳过 ${videos.length} 个视频，` +
        `仅上传 ${images.length} 张图片。是否继续？`,
    );
    if (!ok) return;
  }
  const group = await resolveUploadGroup(getState().albumGroup, 'album');
  if (!group) { toast('无可用目标群（请先在群组 Tab 加载群列表）', 'warn'); return; }
  let ok = 0;
  const failed = [];
  for (const f of images) {
    try {
      // Client-side media type guess; the backend re-detects from bytes.
      const isVideo = /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i.test(f.name);
      const mode = isVideo ? 'video' : 'image';
      const convertOk = (isVideo && ['mp4', 'mkv', 'webm'].includes(albumUploadOptions.convertTo))
        || (!isVideo && ['png', 'jpg', 'jpeg', 'webp'].includes(albumUploadOptions.convertTo));
      const r = await uploadOnce(group, { file: f }, {
        mode,
        to_album: true,
        album_name: albumUploadOptions.albumName || 'AstrBotCloud',
        convert_to: convertOk ? albumUploadOptions.convertTo : undefined,
        lossy: Boolean(albumUploadOptions.lossy),
        lossy_level: albumUploadOptions.lossy || undefined,
      });
      if (r.ok) ok++;
      else failed.push(`${f.name}: ${r.error || 'prepare 被拒绝'}`);
    } catch (e) {
      failed.push(`${f.name}: ${e.message || e}`);
    }
  }
  if (failed.length) {
    toast(`${ok}/${images.length} 个媒体上传成功；失败: ${failed.join('；')}`, 'warn');
  } else if (ok > 0) {
    toast(`${ok}/${images.length} 个媒体上传成功`, 'success');
  }
  if (ok > 0) refresh('albums');
}
