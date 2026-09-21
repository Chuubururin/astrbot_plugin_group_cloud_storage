/**
 * Upload core - the single two-phase upload protocol (B5/B6, F17-F19).
 *
 * Every local-file upload path funnels through:
 *   1) files/upload/prepare -> {token, group}
 *   2) files/upload/<token>  -> bridge multipart upload
 * Long-file sharding, long-video splitting and essence text sharding are
 * server-side responsibilities; this module only forwards the user's
 * explicit ingest mode and optional format conversion target.
 *
 * @module features/upload
 */

import { getState, refresh } from '../store.js';
import { API, apiGet, apiPost, upload as bridgeUpload } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import {
  TEXT_MODE_OPTIONS, VIDEO_MODE_OPTIONS, convertAllowed, convertOptionsFor,
  kindsOf, limitLabel, mediaKind, partitionUploadable, sanitizeUploadName,
  uniqueNames,
} from './ingest-options.js';
import { VOLUME_BYTES } from '../constants.js';

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
    // 「没有可推荐的群」与「请求失败」必须区分：403（群归属账号离线/群已
    // 解散）和网络故障曾与前者一样显示为「请先选择群」，掩盖了真实原因。
    toast(`推荐群获取失败: ${(e && e.message) || e}`, 'error');
    return '';
  }
}

/**
 * Converted upload name: the backend rewrites the staged name to
 * `<stem><convert_to>` and the netdisk relay locates the group-file copy by
 * exact name, so every convert_to call site must submit the target
 * extension instead of the original name.
 * @param {string} name
 * @param {string} convertTo - target extension without the dot ('' = keep)
 * @returns {string}
 */
export function convertTargetName(name, convertTo) {
  const n = String(name || '');
  if (!convertTo) return n;
  return `${n.replace(/\.[^.]+$/, '')}.${convertTo}`;
}

/**
 * One two-phase upload round: prepare -> bridge multipart upload.
 *
 * Shared by every local-file upload path (files tab, album, essence text,
 * netdisk relay).
 *
 * @param {string} group - target group
 * @param {{file: File|Blob, name?: string, size?: number}} spec - payload;
 *   name/size default from file (rename support)
 * @param {Object} [prepareOpts] - extra prepare fields (folder/mode/to_album/
 *   convert_to/lossy)
 * @param {{apiPost?: Function, upload?: Function}} [deps] - injectable IO for
 *   relay callers driven by stubs in unit tests (netdisk-upload)
 * @returns {Promise<{ok: boolean, error?: string, prep: Object|null,
 *   result: Object|null}>} ok=false means the round was refused (no token or
 *   no task id); transport errors throw
 */
export async function uploadOnce(group, spec, prepareOpts = {}, deps = {}) {
  const post = deps.apiPost || apiPost;
  const upload = deps.upload || bridgeUpload;
  const prep = await post(API.FILES.UPLOAD_PREPARE, {
    group,
    name: spec.name || spec.file.name,
    size: spec.size ?? spec.file.size,
    ...prepareOpts,
  });
  if (!prep?.token) {
    // prepare 拒绝原因必须透传：调用方读 r.error 展示（4xx body 形状
    // 因桥实现而异，error/message 双字段兜底）。
    return {
      ok: false,
      error: prep?.error || prep?.message || 'prepare 被拒绝',
      prep: prep || null,
      result: null,
    };
  }
  const result = await upload(`${API.FILES.UPLOAD}/${prep.token}`, spec.file);
  // 第二阶段契约：成功响应一定含 task_id（上传始终异步入 OpQueue，没有
  // 同步完成分支）。缺失即失败——否则 400/异常体会被调用方当成成功累加。
  if (!result?.task_id) {
    return {
      ok: false,
      error: result?.error || result?.message || '上传未返回 task_id（任务未入队）',
      prep,
      result: result || null,
    };
  }
  return { ok: true, prep, result };
}

/**
 * Ask one combined form for media/text ingest choices.
 *
 * @param {Array<{file: File, name: string}>} fileArr
 * @returns {Promise<{videoMode: string, textMode: string, convertTo: string}|null>}
 */
async function askIngestModes(fileArr) {
  const kinds = kindsOf(fileArr);
  const hasVideo = kinds.includes('video');
  const hasText = kinds.includes('text');
  if (!kinds.length) return { videoMode: 'auto', textMode: 'auto', convertTo: '' };

  const fields = [];
  if (hasVideo) {
    fields.push({ name: 'videoMode', label: '视频导入方式', type: 'select', value: 'auto', options: VIDEO_MODE_OPTIONS });
  }
  if (hasText) {
    fields.push({ name: 'textMode', label: '文本导入方式', type: 'select', value: 'auto', options: TEXT_MODE_OPTIONS });
  }
  // Format conversion is offered only for the media kinds actually selected
  // (a text-only batch renders no format field: any target would be dropped).
  const convertOptions = convertOptionsFor(kinds);
  if (convertOptions.length) {
    fields.push({ name: 'convertTo', label: '格式转换', type: 'select', value: '', options: convertOptions });
  }

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
 * Rejects what the backend would refuse (fetch_max_size / 0 bytes), warns
 * about volume splitting, resolves the destination group, asks for
 * ingest/conversion options once, applies the batch naming template, then
 * uploads file by file.
 *
 * @param {FileList|File[]} files
 */
export async function handleFileUpload(files) {
  const fileArr = Array.from(files);
  if (!fileArr.length) return;

  const { usable, rejected, volumes } = partitionUploadable(fileArr);
  if (rejected.length) {
    const detail = rejected.map((r) => `${r.file.name}（${r.reason}）`).join('、');
    toast(`已跳过 ${rejected.length} 个文件: ${detail}`,
      rejected.length === fileArr.length ? 'error' : 'warn');
  }
  if (!usable.length) return;
  if (volumes.length) {
    toast(`${volumes.length} 个文件超过 ${limitLabel(VOLUME_BYTES)}，将按后端 volume_threshold 分卷上传`, 'info');
  }

  let group = getState().currentGroup;
  if (!group) {
    const totalSize = usable.reduce((s, f) => s + (f.size || 0), 0);
    group = await resolveUploadGroup('', 'file', totalSize);
    if (!group) { toast('请先选择群（或等待推荐群可用）', 'warn'); return; }
  }

  const modes = await askIngestModes(usable.map((f) => ({ file: f, name: f.name })));
  if (!modes) return;

  const named = await batchNaming(usable);
  if (!named) return;

  let success = 0;
  const failed = [];
  const albumSkipped = [];
  const folder = getState().folder || '';
  for (const file of named) {
    const kind = mediaKind(file.name);
    // NapCat's album upload is image-only (retcode=100, "群相册上传仅支持
    // JPEG、PNG、GIF、WebP 或 BMP 图片" — verified on the live protocol
    // 2026-09-16), so a `video_album` choice cannot be honoured. Skip the video
    // and say why rather than queueing a task the protocol always rejects.
    if (kind === 'video' && modes.videoMode === 'video_album') {
      albumSkipped.push(file.name);
      continue;
    }
    try {
      const mode = kind === 'video' && modes.videoMode !== 'auto'
        ? 'video'
        : (kind === 'text' && modes.textMode !== 'auto' ? modes.textMode : undefined);
      const convertOk = convertAllowed(kind, modes.convertTo);
      const r = await uploadOnce(group, {
        file: file.file,
        name: convertTargetName(file.name, convertOk ? modes.convertTo : ''),
      }, {
        folder,
        mode,
        convert_to: convertOk ? modes.convertTo : undefined,
      });
      if (r.ok) success++;
      else failed.push(`${file.name}: ${r.error || 'prepare 被拒绝'}`);
    } catch (e) {
      failed.push(`${file.name}: ${e.message || e}`);
    }
  }
  if (albumSkipped.length) {
    toast(
      `协议端群相册仅支持图片，已跳过 ${albumSkipped.length} 个视频：${albumSkipped.join('、')}`,
      'warn',
    );
  }
  const attempted = named.length - albumSkipped.length;
  if (!attempted) return;
  if (failed.length) {
    toast(`${success}/${attempted} 个文件上传成功；失败: ${failed.join('；')}`, 'warn');
  } else if (success > 0) {
    toast(`${success}/${attempted} 个文件上传成功`, 'success');
  }
  if (success > 0) refresh('files');
}

/**
 * Batch naming panel: one template applies to N files ({n}=1..N).
 *
 * Substitutions use replacement functions - a replacement string would
 * interpret `$&`, `` $` ``, `$'`, `$$` and `$1` (a file named `报告$&v2.pdf`
 * rendered as `报告{name}v2.pdf`). Rendered names are then sanitized
 * (reserved characters, 1..80 length) and de-duplicated.
 * @param {File[]} fileArr
 * @returns {Promise<Array<{file: File, name: string}>|null>} named list, null when cancelled
 */
async function batchNaming(fileArr) {
  if (fileArr.length < 2) {
    return [{ file: fileArr[0], name: sanitizeUploadName(fileArr[0].name, fileArr[0].name) }];
  }
  const res = await showFormModal('批量命名', [
    { name: 'template', label: '命名模板', value: '{name}', placeholder: '{name} 原名 / {n} 序号 / {ext} 扩展名' },
  ], { okText: '开始上传' });
  if (!res) return null;
  const template = res.template || '{name}';
  const extOf = (n) => {
    const i = n.lastIndexOf('.');
    return i > -1 ? n.slice(i) : '';
  };
  const rendered = fileArr.map((f, i) => String(template)
    .replace(/\{name\}/g, () => f.name)
    .replace(/\{n\}/g, () => String(i + 1))
    .replace(/\{ext\}/g, () => extOf(f.name)));
  const safe = rendered.map((n, i) => sanitizeUploadName(n, fileArr[i].name));
  const uniq = uniqueNames(safe);
  return fileArr.map((f, i) => ({ file: f, name: uniq[i] }));
}
