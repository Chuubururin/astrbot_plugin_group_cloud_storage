/**
 * Upload core - the single two-phase upload protocol (B5/B6, F17-F19).
 *
 * Every local-file upload path funnels through:
 *   1) files/upload/prepare -> {token, group}
 *   2) files/upload/<token>  -> bridge multipart upload
 * Long-file sharding, long-video splitting and essence text sharding are
 * server-side responsibilities; this module only forwards the user's
 * explicit ingest mode and optional W2-B format conversion target.
 *
 * @module features/upload
 */

import { getState, refresh } from '../store.js';
import { API, apiGet, apiPost, upload as bridgeUpload } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';

const VIDEO_EXT = /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i;
const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|gif)$/i;
const TEXT_EXT = /\.(txt|md)$/i;

/**
 * Resolve the upload target group for a module.
 *
 * Explicit module focus wins; otherwise the backend recommends the
 * smallest group id with enough free space (owner rule 4).
 *
 * @param {string} focusGroup - module-scoped group (albumGroup/essenceGroup)
 * @param {string} kind - 'file' | 'album' | 'essence'
 * @param {number} [size] - payload size hint for capacity-aware recommendation
 * @returns {Promise<string>} group id, '' when unavailable
 */
export async function resolveUploadGroup(focusGroup, kind, size = 0) {
  if (focusGroup) return focusGroup;
  try {
    const rec = await apiGet(API.FILES.RECOMMEND_GROUP, { kind, size });
    return (rec && rec.recommended && rec.recommended.group_id) || '';
  } catch (e) {
    return '';
  }
}

/**
 * Ask one combined form for media/text ingest choices.
 *
 * @param {Array<{file: File, name: string}>} fileArr
 * @returns {Promise<{videoMode: string, textMode: string, convertTo: string}|null>}
 */
async function askIngestModes(fileArr) {
  const hasVideo = fileArr.some((f) => VIDEO_EXT.test(f.name));
  const hasImage = fileArr.some((f) => IMAGE_EXT.test(f.name));
  const hasText = fileArr.some((f) => TEXT_EXT.test(f.name));
  if (!hasVideo && !hasImage && !hasText) return { videoMode: 'auto', textMode: 'auto', convertTo: '' };

  const fields = [];
  if (hasVideo) {
    fields.push({
      name: 'videoMode', label: '视频入库方式', type: 'select', value: 'auto',
      options: [
        { value: 'auto', label: '自动（超限分段入群文件）' },
        { value: 'video', label: '入群文件' },
        { value: 'video_album', label: '入群相册' },
      ],
    });
  }
  if (hasText) {
    fields.push({
      name: 'textMode', label: '文本入库方式', type: 'select', value: 'auto',
      options: [
        { value: 'auto', label: '自动' },
        { value: 'text', label: '文本入精华' },
        { value: 'file', label: '入群文件' },
      ],
    });
  }
  // W2-B: one conversion target for the whole batch. The option list is
  // narrowed by the media kinds actually selected.
  const isOnlyVideo = hasVideo && !hasImage;
  const isOnlyImage = hasImage && !hasVideo;
  fields.push({
    name: 'convertTo', label: '格式转换', type: 'select', value: '',
    options: [
      { value: '', label: '保持原格式' },
      ...(isOnlyImage
        ? [{ value: 'png', label: 'PNG' }, { value: 'jpg', label: 'JPG' }, { value: 'webp', label: 'WebP' }]
        : isOnlyVideo
          ? [{ value: 'mp4', label: 'MP4' }, { value: 'mkv', label: 'MKV' }, { value: 'webm', label: 'WebM' }]
          : []),
      ...(!isOnlyImage && !isOnlyVideo
        ? [{ value: 'mp4', label: '转为 MP4' }, { value: 'png', label: '转为 PNG' }, { value: 'jpg', label: '转为 JPG' }]
        : []),
    ],
  });

  const res = await showFormModal('上传选项', fields, { okText: '继续' });
  if (!res) return null;
  return {
    videoMode: res.videoMode || 'auto',
    textMode: res.textMode || 'auto',
    convertTo: res.convertTo || '',
  };
}

/**
 * Handle a batch of selected local files for the files tab.
 *
 * Resolves the destination group, asks for ingest/conversion options once,
 * applies the batch naming template, then uploads file by file.
 *
 * @param {FileList|File[]} files
 */
export async function handleFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;

  let group = getState().currentGroup;
  if (!group) {
    const totalSize = fileArr.reduce((s, f) => s + (f.size || 0), 0);
    group = await resolveUploadGroup('', 'file', totalSize);
    if (!group) { toast('请先选择群（或等待推荐群可用）', 'warn'); return; }
  }

  const modes = await askIngestModes(fileArr.map((f) => ({ file: f, name: f.name })));
  if (!modes) return;

  const named = await batchNaming(fileArr);
  if (!named) return;

  let success = 0;
  const folder = getState().folder || '';
  for (const file of named) {
    try {
      const isVideo = VIDEO_EXT.test(file.name);
      const isText = TEXT_EXT.test(file.name);
      // 'auto' leaves the backend default (direct group-file upload).
      const mode = isVideo && modes.videoMode !== 'auto'
        ? (modes.videoMode === 'video_album' ? 'video' : modes.videoMode)
        : (isText && modes.textMode !== 'auto' ? modes.textMode : undefined);
      const convertOk = (isVideo && ['mp4', 'mkv', 'webm'].includes(modes.convertTo))
        || (IMAGE_EXT.test(file.name) && ['png', 'jpg', 'jpeg', 'webp'].includes(modes.convertTo));
      const prep = await apiPost(API.FILES.UPLOAD_PREPARE, {
        group,
        name: file.name,
        size: file.size,
        folder,
        mode,
        to_album: isVideo && modes.videoMode === 'video_album',
        convert_to: convertOk ? modes.convertTo : undefined,
      });
      if (prep?.token) {
        await bridgeUpload(`${API.FILES.UPLOAD}/${prep.token}`, file.file);
        success++;
      }
    } catch (e) {
      toast(`上传失败: ${file.name}`, 'error');
    }
  }
  if (success > 0) {
    toast(`${success}/${named.length} 个文件上传成功`, 'success');
    refresh('files');
  }
}

/**
 * Batch naming panel: one template applies to N files ({n}=1..N).
 * @param {File[]} fileArr
 * @returns {Promise<Array<{file: File, name: string}>|null>} named list, null when cancelled
 */
async function batchNaming(fileArr) {
  if (fileArr.length < 2) return fileArr.map((f) => ({ file: f, name: f.name }));
  const res = await showFormModal('批量命名', [
    { name: 'template', label: '命名模板', value: '{name}', placeholder: '{name} 原名 / {n} 序号 / {ext} 扩展名' },
  ], { okText: '开始上传' });
  if (!res) return null;
  const template = res.template || '{name}';
  const extOf = (n) => {
    const i = n.lastIndexOf('.');
    return i > -1 ? n.slice(i) : '';
  };
  return fileArr.map((f, i) => {
    let name = template
      .replace(/\{name\}/g, f.name)
      .replace(/\{n\}/g, String(i + 1))
      .replace(/\{ext\}/g, extOf(f.name))
      .trim();
    if (!name) name = f.name;
    return { file: f, name };
  });
}
