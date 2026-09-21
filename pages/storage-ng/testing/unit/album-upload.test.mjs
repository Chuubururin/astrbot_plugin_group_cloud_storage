/**
 * Unit tests: album upload is image-only.
 *
 * Regression guard for the 2026-09-16 revert. An earlier round treated the
 * album entries' local video skip as a stale self-imposed restriction and let
 * videos through as `mode=video` + `to_album=true`. The live protocol
 * disproved that: NapCat's `upload_image_to_qun_album` answers a video with
 * retcode=100 "群相册上传仅支持 JPEG、PNG、GIF、WebP 或 BMP 图片", and no
 * adapter exposes an album-video action at all (ports/capabilities.py
 * AlbumCapability has no upload method). So these tests pin:
 *   - a video is dropped locally with a reason, never queued;
 *   - an image still goes out as `mode=image` + `to_album=true`;
 *   - a mixed batch submits only its images;
 *   - no option table promises a usable album-video path.
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { handleAlbumFileUpload } from '../../features/ingest.js';
import { ALBUM_SOURCE_OPTIONS, VIDEO_MODE_OPTIONS } from '../../features/ingest-options.js';
import { API } from '../../api.js';

// Minimal DOM stubs (the ingest modules pull in toast/modal/store).
/** Every node the stub hands out, so a rendered toast can be read back (T10). */
const created = [];
const makeEl = () => ({
  className: '', classList: { add() {}, remove() {}, toggle() {} },
  dataset: {}, children: [], style: {}, innerHTML: '', textContent: '',
  appendChild() {}, remove() {}, addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, setAttribute() {}, click() {},
});
globalThis.document = {
  body: makeEl(),
  createElement: () => { const node = makeEl(); created.push(node); return node; },
  getElementById: () => null,
  addEventListener() {}, querySelector() { return null; },
  querySelectorAll: () => [], readyState: 'complete',
};
/** Toast messages rendered so far (components/toast.js builds .toast-msg nodes). */
const toastMessages = () => created
  .filter((el) => el.className === 'toast-msg').map((el) => el.textContent);

globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

/** Capture every prepare body; answer both upload phases. */
function stubSdk() {
  const prepares = [];
  globalThis.window = {
    AstrBotPluginPage: {
      apiGet: async (p) => (p === API.FILES.RECOMMEND_GROUP
        ? { recommended: { group_id: '20002' } } : {}),
      apiPost: async (p, body) => {
        if (p === API.FILES.UPLOAD_PREPARE) { prepares.push(body); return { token: 'tok' }; }
        return {};
      },
      upload: async () => ({ task_id: 't1' }),
    },
  };
  return prepares;
}

const file = (name, size = 10) => ({ name, size, type: '' });

test('album upload: a video is dropped locally with a reason, never queued as mode=video', async () => {
  const prepares = stubSdk();
  created.length = 0;
  try {
    await handleAlbumFileUpload([file('clip.mp4', 4096)]);
    assert.deepEqual(prepares, [], 'the protocol rejects video, so nothing may be submitted');
    // T10: the drop must come with a reason. Deleting the notice (a silent
    // skip) has to turn this test red - `prepares == []` alone cannot tell
    // "refused with an explanation" from "dropped on the floor".
    const notices = toastMessages();
    assert.equal(notices.length, 1, `exactly one drop notice, got ${JSON.stringify(notices)}`);
    assert.match(notices[0], /视频/, 'the notice must name what was dropped');
    assert.match(notices[0], /相册|仅支持图片/, 'the notice must give the reason');
  } finally { delete globalThis.window; }
});

test('album upload: an image goes out as mode=image + to_album', async () => {
  const prepares = stubSdk();
  try {
    await handleAlbumFileUpload([file('pic.png')]);
    assert.equal(prepares.length, 1);
    assert.equal(prepares[0].mode, 'image');
    assert.equal(prepares[0].to_album, true);
    assert.equal(prepares[0].group, '20002');
    assert.equal(prepares[0].name, 'pic.png');
  } finally { delete globalThis.window; }
});

test('album upload: a mixed batch submits only its images', async () => {
  const prepares = stubSdk();
  try {
    await handleAlbumFileUpload([file('a.mp4'), file('b.jpg'), file('c.webm')]);
    assert.deepEqual(prepares.map((b) => b.mode), ['image']);
    assert.deepEqual(prepares.map((b) => b.name), ['b.jpg']);
    assert.ok(prepares.every((b) => b.to_album === true));
  } finally { delete globalThis.window; }
});

test('album upload: non-media is refused locally, never sent as mode=image', async () => {
  const prepares = stubSdk();
  try {
    // mode=image would 400 ("image mode only accepts image extensions").
    await handleAlbumFileUpload([file('notes.txt'), file('doc.pdf')]);
    assert.deepEqual(prepares, []);
  } finally { delete globalThis.window; }
});

test('album upload: no entry or option advertises a working album-video path', () => {
  for (const opt of ALBUM_SOURCE_OPTIONS) {
    assert.ok(
      !/视频|video/i.test(opt.label),
      `${opt.value} must not offer video: ${opt.label}`,
    );
  }
  // `video_album` stays listed so the reason remains visible, but its label
  // must state the protocol limit rather than implying it works.
  const albumMode = VIDEO_MODE_OPTIONS.find((o) => o.value === 'video_album');
  assert.ok(albumMode, 'the album video mode stays listed for the explanation');
  assert.match(albumMode.label, /仅支持图片|不可用/, `label must state the limit: ${albumMode.label}`);
});
