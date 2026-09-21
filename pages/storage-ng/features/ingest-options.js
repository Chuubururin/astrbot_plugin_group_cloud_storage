/**
 * Ingest options and hard-limit helpers - the single home for the option
 * tables and the platform limits shared by every upload entry.
 *
 * constants.js carries the machine values (they mirror the backend config);
 * this module turns them into the decisions the entries actually need:
 * which format targets a batch may offer, which files must be rejected
 * before submitting, and when the backend will split/shard the payload.
 * Pure data and pure functions only - no DOM, no store, no HTTP.
 *
 * @module features/ingest-options
 */

import {
  ESSENCE_CHUNK_CHARS, MAX_UPLOAD_SIZE, VOLUME_BYTES,
} from '../constants.js';

// ---- Media type detection (client-side guess; the backend re-detects) ----
export const VIDEO_EXT = /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i;
export const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|gif)$/i;
export const TEXT_EXT = /\.(txt|md)$/i;

/** prepare contract: name length 1..80 */
export const NAME_MAX_LEN = 80;

/** Machine values accepted by the prepare `convert_to` whitelist. */
export const VIDEO_TARGETS = ['mp4', 'mkv', 'webm'];
export const IMAGE_TARGETS = ['png', 'jpg', 'jpeg', 'webp'];

/** Lossy re-encode tiers (album only; machine values, labels in the UI). */
export const LOSSY_LEVELS = ['high', 'medium', 'low'];

/** Media kind of a file name: 'video' | 'image' | 'text' | '' (unknown). */
export function mediaKind(name) {
  const n = String(name || '');
  if (VIDEO_EXT.test(n)) return 'video';
  if (IMAGE_EXT.test(n)) return 'image';
  if (TEXT_EXT.test(n)) return 'text';
  return '';
}

/** Distinct media kinds present in a file batch. */
export function kindsOf(files) {
  return [...new Set((files || []).map((f) => mediaKind(f.name)).filter(Boolean))];
}

/** Whether a conversion target is legal for a media kind. */
export function convertAllowed(kind, convertTo) {
  if (!convertTo) return false;
  if (kind === 'video') return VIDEO_TARGETS.includes(convertTo);
  if (kind === 'image') return IMAGE_TARGETS.includes(convertTo);
  return false;
}

/**
 * Validate a conversion target against a known media kind. An unknown kind
 * passes through (the backend re-detects from bytes); a known mismatch is
 * dropped so an illegal combination never reaches prepare (400).
 * @param {string} kind
 * @param {string} convertTo
 * @returns {string} usable target ('' = keep the original format)
 */
export function resolveConvertTo(kind, convertTo) {
  const target = String(convertTo || '');
  if (!target || !kind) return target;
  return convertAllowed(kind, target) ? target : '';
}

/**
 * Format-conversion select options narrowed to the media kinds in a batch.
 * Returns [] when nothing in the batch is convertible (text only), so the
 * caller renders no format field at all.
 * @param {string[]} kinds - subset of 'video'|'image'
 * @returns {Array<{value: string, label: string}>}
 */
export function convertOptionsFor(kinds) {
  const has = (k) => (kinds || []).includes(k);
  const out = [];
  if (has('video')) {
    out.push({ value: 'mp4', label: '转为 MP4' });
    out.push({ value: 'mkv', label: '转为 MKV' });
    out.push({ value: 'webm', label: '转为 WebM' });
  }
  if (has('image')) {
    out.push({ value: 'png', label: '转为 PNG' });
    out.push({ value: 'jpg', label: '转为 JPG' });
    out.push({ value: 'webp', label: '转为 WebP' });
  }
  return out.length ? [{ value: '', label: '保持原格式' }, ...out] : [];
}

/** Last path segment of a URL (extension source when no filename is given). */
export function nameFromUrl(url, filename) {
  if (filename) return String(filename);
  const path = String(url || '').split(/[?#]/)[0];
  return path.slice(path.lastIndexOf('/') + 1);
}

/** Human-readable byte label using the binary base the limits declare. */
export function limitLabel(bytes) {
  const mb = Math.round(Number(bytes || 0) / (1024 * 1024));
  return mb >= 1024 ? `${Math.round(mb / 1024)}GB` : `${mb}MB`;
}

/**
 * Pre-flight gate for a local file batch: reject what the backend refuses
 * (fetch_max_size) or cannot accept (0 bytes), and flag volume-sized files
 * that the backend will split.
 * @param {File[]} files
 * @returns {{usable: File[], rejected: Array<{file: File, reason: string}>,
 *   volumes: File[]}}
 */
export function partitionUploadable(files) {
  const usable = [];
  const rejected = [];
  for (const f of files || []) {
    const size = f.size || 0;
    if (size <= 0) {
      rejected.push({ file: f, reason: '空文件（0 字节）' });
    } else if (size > MAX_UPLOAD_SIZE) {
      rejected.push({
        file: f,
        reason: `超过客户端上限 ${limitLabel(MAX_UPLOAD_SIZE)}（后端 fetch_max_size）`,
      });
    } else {
      usable.push(f);
    }
  }
  return { usable, rejected, volumes: usable.filter((f) => (f.size || 0) > VOLUME_BYTES) };
}

const ILLEGAL_NAME_CHARS = /[\\/:*?"<>|\u0000-\u001f]/g;

/**
 * Sanitize one upload name: strip path separators and reserved characters,
 * keep the extension, cap at the prepare contract length.
 * @param {string} name - rendered/typed name
 * @param {string} fallback - used when nothing usable remains
 * @returns {string}
 */
export function sanitizeUploadName(name, fallback) {
  const cleaned = String(name == null ? '' : name).replace(ILLEGAL_NAME_CHARS, '').trim();
  const safe = cleaned || String(fallback == null ? '' : fallback).trim();
  if (safe.length <= NAME_MAX_LEN) return safe;
  const dot = safe.lastIndexOf('.');
  const ext = dot > 0 ? safe.slice(dot) : '';
  const stem = dot > 0 ? safe.slice(0, dot) : safe;
  // 超长扩展名（ext 比上限还长）时 stem 被压到 1 字符，拼回后总长仍超限：
  // 必须按拼接后的总长再截一次（prepare 契约 name 1..80）。
  return (stem.slice(0, Math.max(1, NAME_MAX_LEN - ext.length)) + ext).slice(0, NAME_MAX_LEN);
}

/**
 * Make batch names unique: group files are name-addressed, so two files
 * rendered to the same name (template `{name}` with same-named sources)
 * would overwrite/ambiguate each other.
 * @param {string[]} names
 * @returns {string[]}
 */
export function uniqueNames(names) {
  const used = new Set();
  return (names || []).map((n) => {
    let candidate = n;
    let i = 1;
    while (used.has(candidate.toLowerCase())) {
      i += 1;
      const suffix = `(${i})`;
      const dot = n.lastIndexOf('.');
      const ext = dot > 0 ? n.slice(dot) : '';
      const stem = dot > 0 ? n.slice(0, dot) : n;
      // 后缀在长度截断之后追加会把结果重新推过上限（prepare 契约 1..80）：
      // 先把 stem 截到「上限 - 后缀 - 扩展名」，再整体兜底截断。
      candidate = (stem.slice(0, Math.max(1, NAME_MAX_LEN - ext.length - suffix.length))
        + suffix + ext).slice(0, NAME_MAX_LEN);
    }
    used.add(candidate.toLowerCase());
    return candidate;
  });
}

/**
 * Essence sharding notice for browser-entered text.
 * @param {string} text
 * @returns {string} '' when the text fits a single essence message
 */
export function essenceChunkNotice(text) {
  const len = String(text || '').length;
  if (len <= ESSENCE_CHUNK_CHARS) return '';
  const parts = Math.ceil(len / ESSENCE_CHUNK_CHARS);
  return `文本 ${len} 字超过精华分片阈值 ${ESSENCE_CHUNK_CHARS} 字（配置 essence_chunk_size），`
    + `将自动拆分为约 ${parts} 条精华消息`;
}

// ---- Shared option tables (machine value + user-facing label) ----

/** Files-tab upload sources. */
export const UPLOAD_SOURCE_OPTIONS = [
  { value: 'local', label: '本地文件' },
  { value: 'url', label: 'URL 链接' },
  { value: 'text', label: '从浏览器上传文本' },
  { value: 'netdisk', label: '由网盘上传' },
  { value: 'album', label: '由相册上传（仅图片）' },
  { value: 'essence', label: '由精华上传（文本）' },
];

/**
 * Album-tab upload sources.
 *
 * Images only. NapCat's `upload_image_to_qun_album` rejects video outright
 * (retcode=100, "群相册上传仅支持 JPEG、PNG、GIF、WebP 或 BMP 图片"), verified
 * against the live protocol on 2026-09-16 — so the entries never submit one.
 */
export const ALBUM_SOURCE_OPTIONS = [
  { value: 'local', label: '本地图片（相册仅接受图片）' },
  { value: 'url', label: 'URL 链接（图片）' },
  { value: 'netdisk', label: '由网盘上传（图片）' },
];

/** Essence-tab upload sources. */
export const ESSENCE_SOURCE_OPTIONS = [
  { value: 'input', label: '浏览器输入文本' },
  { value: 'file', label: '文档（文件）读取' },
  { value: 'url', label: 'URL 访问读取' },
  { value: 'netdisk', label: '网盘文档读取' },
];

/** Lossy re-encode tiers (album only, irreversible). */
export const LOSSY_OPTIONS = [
  { value: '', label: '不压缩' },
  { value: 'high', label: '轻度（画质优先）' },
  { value: 'medium', label: '均衡' },
  { value: 'low', label: '强力（体积优先）' },
];

/**
 * Video ingest mode (files tab).
 *
 * `video_album` stays listed but is not usable: the album pipeline is intact
 * server-side, yet NapCat's album upload accepts images only (see
 * ALBUM_SOURCE_OPTIONS), so the entries refuse locally instead of queueing a
 * task the protocol is guaranteed to reject. The option is kept so the reason
 * stays visible and a supporting adapter can re-enable it without a rewrite.
 */
export const VIDEO_MODE_OPTIONS = [
  { value: 'auto', label: '自动（超限分段入群文件）' },
  { value: 'video', label: '入群文件' },
  { value: 'video_album', label: '入群相册（协议端仅支持图片，暂不可用）' },
];

/** Text ingest mode (files tab). */
export const TEXT_MODE_OPTIONS = [
  { value: 'auto', label: '自动' },
  { value: 'text', label: '文本入精华' },
  { value: 'file', label: '入群文件' },
];
