/**
 * Unit tests: upload flows (W3-C segmented source entry, two-phase
 * prepare/upload, netdisk local upload two-step relay W3-D).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { uploadFilesToNetdisk, waitTasksDone } from '../../features/netdisk-upload.js';
import { handleFileUpload, resolveUploadGroup } from '../../features/upload.js';
import { showUploadSourceModal } from '../../features/ingest.js';
import { API } from '../../api.js';

// Minimal DOM stubs (upload entry modules import toast/modal/store).
const makeEl = () => ({
  className: '', classList: { add() {}, remove() {}, toggle() {} },
  dataset: {}, children: [], style: {}, innerHTML: '', textContent: '',
  appendChild() {}, remove() {}, addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, setAttribute() {}, click() {},
});
globalThis.document = {
  body: makeEl(),
  createElement: () => makeEl(),
  getElementById: () => null,
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll: () => [],
  readyState: 'complete',
};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

function fakeDeps(overrides = {}) {
  return {
    apiPost: async (p) => {
      if (p === API.FILES.UPLOAD_PREPARE) return { token: 'tok1' };
      if (p === API.FILES.RECOMMEND_GROUP) return { recommended: { group_id: '20002' } };
      if (p === API.TASKS) return { tasks: [{ task_id: 'x1', state: 'done' }] };
      if (p === API.BRIDGE.TRANSFER) return { ok: true };
      return {};
    },
    apiGet: async (p, q) => {
      // 2026-09-03 修复：recommend-group 走 GET（apiGet）
      if (p === API.FILES.RECOMMEND_GROUP) return { recommended: { group_id: '20002' } };
      if (p === API.FILES.LIST) {
        const name = (q && q.q) || 'a.txt';
        return { items: [{ id: 7, name, is_dir: false }] };
      }
      return {};
    },
    upload: async () => ({ task_id: 'x1' }),
    group: '10001',
    ...overrides,
  };
}

test('netdisk-upload: two-step relay transfers after task done (W3-D)', async () => {
  const deps = fakeDeps();
  const st = await uploadFilesToNetdisk([
    { name: 'a.txt', size: 10 },
    { name: 'b.txt', size: 5 },
  ], deps);
  assert.equal(st.total, 2);
  assert.equal(st.uploaded, 2);
  assert.equal(st.transferred, 2);
  assert.deepEqual(st.failed, []);
});

test('netdisk-upload: failed upload is reported, not transferred', async () => {
  const deps = fakeDeps({ upload: async () => ({ task_id: '' }) });
  const st = await uploadFilesToNetdisk([{ name: 'c.txt', size: 1 }], deps);
  assert.equal(st.transferred, 0);
  assert.equal(st.failed.length, 1);
  assert.match(st.failed[0], /c\.txt/);
});

test('netdisk-upload: recommended group used when none selected (N-07)', async () => {
  const deps = fakeDeps({ group: '' });
  const st = await uploadFilesToNetdisk([{ name: 'a.txt', size: 1 }], deps);
  assert.equal(st.transferred, 1);
  assert.equal(st.failed.length, 0);
});

test('netdisk-upload: no group available -> error, nothing submitted', async () => {
  const deps = fakeDeps({
    group: '',
    apiGet: async (p) => {
      // 2026-09-03：recommend 走 GET；无可用群 → 空推荐
      if (p === API.FILES.RECOMMEND_GROUP) return {};
      return {};
    },
    apiPost: async (p) => {
      if (p === API.FILES.UPLOAD_PREPARE) return { token: 't' };
      return {};
    },
  });
  await assert.rejects(
    () => uploadFilesToNetdisk([{ name: 'a.txt', size: 1 }], deps),
    /目标群/,
  );
});

test('netdisk-upload: waitTasksDone resolves on terminal states', async () => {
  const done = await waitTasksDone(['x1', 'x2'], {
    apiPost: async () => ({
      tasks: [
        { task_id: 'x1', state: 'done' },
        { task_id: 'x2', state: 'failed' },
      ],
    }),
    group: '10001',
  }, 5000);
  assert.equal(done.get('x1'), 'done');
  assert.equal(done.get('x2'), 'failed');
});

test('upload: five-source segmentation and two-phase helpers exported (W3-C)', () => {
  assert.equal(typeof showUploadSourceModal, 'function');
  assert.equal(typeof handleFileUpload, 'function');
  assert.equal(typeof resolveUploadGroup, 'function');
});

test('upload: resolveUploadGroup falls back to recommend when no focus group', async () => {
  const group = await resolveUploadGroup('', 'file', 100);
  assert.equal(group, '', 'no SDK in node -> recommend fails quietly, returns ""');
});