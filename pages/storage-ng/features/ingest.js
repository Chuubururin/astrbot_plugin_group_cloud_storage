/**
 * Ingest entries - every upload source form .
 *
 * Local uploads use the two-phase prepare/upload protocol from
 * features/upload.js. Cross-tab uploads (netdisk/album/essence -> files,
 * netdisk -> album, netdisk -> essence) delegate to features/cross-upload.js,
 * which drives the existing distribute endpoints.
 *
 * Option tables and the platform-limit helpers live in
 * features/ingest-options.js (pure data, shared with upload.js).
 *
 * @module features/ingest
 */

import { getState, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { mutate } from '../utils/mutate.js';
import { convertTargetName, resolveUploadGroup, uploadOnce } from './upload.js';
import { handleNetdiskUploadLocal } from './netdisk-upload.js';
import {
  ALBUM_SOURCE_OPTIONS, ESSENCE_SOURCE_OPTIONS, LOSSY_OPTIONS, UPLOAD_SOURCE_OPTIONS,
  convertOptionsFor, essenceChunkNotice, mediaKind, nameFromUrl, resolveConvertTo,
} from './ingest-options.js';
import {
  pickNetdiskFile, openNetdiskToGroup, openAlbumToGroup, openEssenceToGroup,
} from './cross-upload.js';

export { handleNetdiskUploadLocal };

/** Options chosen in the album source modal, consumed by the hidden input. */
// Must match the backend default ("AstrBot云盘"): the album is resolved by
// exact name, and creating a missing one needs an extension NapCat lacks.
const DEFAULT_ALBUM_NAME = 'AstrBot云盘';
let albumUploadOptions = { convertTo: '', lossy: '', albumName: DEFAULT_ALBUM_NAME };

/**
 * Album conversion target for one media name. A target that does not match
 * the media type is dropped loudly instead of being forwarded (an illegal
 * combination is a hard 400 in prepare).
 * @param {string} name
 * @returns {string} target extension ('' = keep the original format)
 */
function albumConvertTarget(name) {
  const target = resolveConvertTo(mediaKind(name), albumUploadOptions.convertTo);
  if (albumUploadOptions.convertTo && !target) {
    toast(`媒体类型与所选格式「${albumUploadOptions.convertTo}」不符，已保持原格式`, 'warn');
  }
  return target;
}

/**
 * Files-tab upload entry: one of the five owner-mandated sources.
 * Local opens the hidden picker; the other four execute directly.
 */
export async function showUploadSourceModal() {
  const res = await showFormModal('上传到群文件：选择来源', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: UPLOAD_SOURCE_OPTIONS },
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
    { name: 'convert_to', label: '格式转换（按媒体类型生效）', type: 'select', value: '',
      options: convertOptionsFor(['video', 'image']) },
  ]);
  if (!res?.url) return;
  // 媒体类型在下载前只能按 URL 推断：已知类型与目标不符时丢弃，未知类型
  // 透传（后端按字节重判）。
  const target = resolveConvertTo(mediaKind(nameFromUrl(res.url, res.filename)), res.convert_to);
  if (res.convert_to && !target) {
    toast(`URL 媒体类型与所选格式「${res.convert_to}」不符，已保持原格式`, 'warn');
  }
  await mutate('URL 导入', API.FETCH, {
    group, url: res.url, name: res.filename || '', convert_to: target,
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
  // Server-side long-text sharding is triggered automatically; the client
  // announces the split with the configured essence_chunk_size threshold.
  const chunkNotice = essenceChunkNotice(res.text);
  if (chunkNotice) toast(chunkNotice, 'info');
  await mutate('保存', API.ESSENCE.SAVE, { group, title: res.title || '', text: res.text });
}

/**
 * Album upload entry : local image / URL image / netdisk image.
 * Lossy re-encode is the user's per-upload choice (checkbox + quality
 * tier, irreversible); optional format conversion remains.
 * @param {HTMLElement} fileInput - hidden <input type=file accept=image/*,video/*>
 */
export async function showAlbumUploadModal(fileInput) {
  const res = await showFormModal('上传到相册', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: ALBUM_SOURCE_OPTIONS },
    { name: 'album', label: '相册名', value: DEFAULT_ALBUM_NAME, placeholder: '目标相册名' },
    // 相册仅落图片（协议端限制）：转换目标按图片类型收窄。
    { name: 'convert_to', label: '格式转换（图片）', type: 'select', value: '',
      options: convertOptionsFor(['image']) },
    { name: 'lossy', label: '有损压缩（重编码，不可逆）', type: 'select', value: '', options: LOSSY_OPTIONS },
  ]);
  if (!res?.source) return; // 模态取消（res=null）：与 showUploadSourceModal 同款守卫
  albumUploadOptions = {
    convertTo: res.convert_to || '',
    lossy: res.lossy || '',
    albumName: res.album || DEFAULT_ALBUM_NAME,
  };

  if (res.source === 'netdisk') {
    const file = await pickNetdiskFile();
    if (!file) return;
    if (mediaKind(file.name) !== 'image') {
      toast('相册仅接受图片：协议端群相册不支持视频', 'warn');
      return;
    }
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    await mutate('转存', API.BRIDGE.NETDISK_DISTRIBUTE, {
      path: file.remote_path || file.name, target: 'album', group,
      name: file.name || '',
      convert_to: albumConvertTarget(file.name),
      // 与同文件其他分支同款下发（此前该分支丢弃用户选择的有损压缩）。
      lossy: Boolean(albumUploadOptions.lossy),
      lossy_level: albumUploadOptions.lossy || undefined,
    }, { refresh: 'albums', successText: '网盘→相册转存已提交' });
    return;
  }

  if (res.source === 'url') {
    const group = await resolveUploadGroup(getState().albumGroup, 'album');
    if (!group) { toast('无可用目标群', 'warn'); return; }
    const url = await showFormModal('URL 上传图片', [
      { name: 'url', label: '图片 URL', required: true, placeholder: 'https://...' },
    ]);
    if (!url?.url) return;
    // 相册只接受图片：视频 URL 会被 fetch 路由到 video_album 任务，再由协议端
    // 以 retcode=100 拒绝（2026-09-16 真机证据），因此在此直接拦下。
    if (mediaKind(nameFromUrl(url.url)) === 'video') {
      toast('相册仅接受图片：协议端群相册不支持视频', 'warn');
      return;
    }
    await mutate('相册上传', API.FETCH, {
      group, url: url.url, to_album: true, album_name: albumUploadOptions.albumName || DEFAULT_ALBUM_NAME,
      convert_to: albumConvertTarget(nameFromUrl(url.url)),
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
    { name: 'source', label: '来源', type: 'select', value: 'input', options: ESSENCE_SOURCE_OPTIONS },
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
      const chunkNotice = essenceChunkNotice(res.text);
      if (chunkNotice) toast(chunkNotice, 'info');
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
 * Album media upload: local images via prepare/upload with to_album=true.
 *
 * Videos are refused locally. NapCat's `upload_image_to_qun_album` is
 * image-only (retcode=100, "群相册上传仅支持 JPEG、PNG、GIF、WebP 或 BMP
 * 图片"), verified against the live protocol on 2026-09-16.
 * @param {FileList|File[]} files
 */
export async function handleAlbumFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;
  const images = fileArr.filter((f) => mediaKind(f.name) === 'image');
  const videos = fileArr.filter((f) => mediaKind(f.name) === 'video').length;
  const skipped = fileArr.length - images.length;
  if (skipped) {
    toast(videos === skipped
      ? `已跳过 ${videos} 个视频（协议端群相册仅支持图片）`
      : `已跳过 ${skipped} 个非图片文件（含 ${videos} 个视频）`, 'warn');
  }
  if (!images.length) return;
  const group = await resolveUploadGroup(getState().albumGroup, 'album');
  if (!group) { toast('无可用目标群（请先在群组 Tab 加载群列表）', 'warn'); return; }
  let ok = 0;
  const failed = [];
  for (const f of images) {
    try {
      const convertTo = albumConvertTarget(f.name);
      const r = await uploadOnce(group, { file: f, name: convertTargetName(f.name, convertTo) }, {
        mode: 'image',
        to_album: true,
        album_name: albumUploadOptions.albumName || DEFAULT_ALBUM_NAME,
        convert_to: convertTo || undefined,
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
