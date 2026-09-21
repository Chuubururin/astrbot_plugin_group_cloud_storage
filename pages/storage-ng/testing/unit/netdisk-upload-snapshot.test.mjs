/**
 * Unit tests: netdisk relay - pre-relay id snapshot honesty.
 *
 * L9: snapshotIds() always read page 1 of the group listing. With
 * MAX_PAGE_SIZE files or more the result is truncated, so `preexisting` was an
 * incomplete set: a same-named file living beyond page 1 was not in it, landed
 * in locateByName()'s `fresh` set and got bridged to the netdisk instead of
 * the upload that just finished. A full page is now "unknown" (null), and an
 * unknown snapshot refuses to guess between several same-named candidates
 * (utils/recover.js netdiskRows uses the same rule for netdisk rows).
 *
 * Run: node --test pages/storage-ng/testing/unit/netdisk-upload-snapshot.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { uploadFilesToNetdisk } from '../../features/netdisk-upload.js';
import { API } from '../../api.js';
import { MAX_PAGE_SIZE } from '../../constants.js';

// Minimal DOM stubs (the relay modules pull in toast/modal/store).
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

/** A listing of `n` unrelated files, standing in for one group-listing page. */
const filler = (n) => Array.from({ length: n }, (_, i) => ({
  id: 1000 + i, name: `f${i}.txt`, is_dir: false,
}));

/**
 * @param {Array} snapshot - page 1 of the pre-relay listing
 * @param {Array} hits - the same-named candidates locateByName() will see
 */
function makeDeps(snapshot, hits) {
  const transfers = [];
  const deps = {
    group: '10001',
    apiGet: async (p, q) => {
      if (p !== API.FILES.LIST) return {};
      return q && q.q ? { items: hits } : { items: snapshot };
    },
    apiPost: async (p, body) => {
      if (p === API.FILES.UPLOAD_PREPARE) return { token: 'tok1' };
      // No resource_id on the ledger row: the relay must fall back to the
      // name lookup, which is exactly the path under test.
      if (p === API.TASKS) return { tasks: [{ task_id: 'x1', state: 'done' }] };
      if (p === API.BRIDGE.TRANSFER) { transfers.push(body); return { ok: true }; }
      return {};
    },
    upload: async () => ({ task_id: 'x1' }),
  };
  return { deps, transfers };
}

test('L9: a truncated snapshot never transfers the old same-named file', async () => {
  // The pre-existing a.txt sits beyond page 1, so the snapshot cannot see it.
  const hits = [{ id: 1, name: 'a.txt', is_dir: false }, { id: 2, name: 'a.txt', is_dir: false }];
  const { deps, transfers } = makeDeps(filler(MAX_PAGE_SIZE), hits);
  const st = await uploadFilesToNetdisk([{ name: 'a.txt', size: 10 }], deps);
  assert.deepEqual(transfers, [], 'an ambiguous same-name match must not be transferred');
  assert.ok(st.failed.some((f) => f.includes('定位失败')),
    `the locate failure must be reported: ${JSON.stringify(st.failed)}`);
});

test('L9: a truncated snapshot still transfers an unambiguous match', async () => {
  const hits = [{ id: 2, name: 'a.txt', is_dir: false }];
  const { deps, transfers } = makeDeps(filler(MAX_PAGE_SIZE), hits);
  const st = await uploadFilesToNetdisk([{ name: 'a.txt', size: 10 }], deps);
  assert.deepEqual(transfers.map((b) => b.resource_ids), [[2]]);
  assert.equal(st.transferred, 1);
});

test('L9: a complete snapshot still prefers the id that did not exist before', async () => {
  const snapshot = [{ id: 1, name: 'a.txt', is_dir: false }];
  const hits = [{ id: 1, name: 'a.txt', is_dir: false }, { id: 2, name: 'a.txt', is_dir: false }];
  const { deps, transfers } = makeDeps(snapshot, hits);
  const st = await uploadFilesToNetdisk([{ name: 'a.txt', size: 10 }], deps);
  assert.deepEqual(transfers.map((b) => b.resource_ids), [[2]],
    'the pre-existing id must never be mistaken for the fresh upload');
  assert.equal(st.transferred, 1);
});

test('L9: a snapshot one short of a full page is still trusted', async () => {
  const snapshot = [{ id: 1, name: 'a.txt', is_dir: false }, ...filler(MAX_PAGE_SIZE - 2)];
  assert.equal(snapshot.length, MAX_PAGE_SIZE - 1);
  const hits = [{ id: 1, name: 'a.txt', is_dir: false }, { id: 2, name: 'a.txt', is_dir: false }];
  const { deps, transfers } = makeDeps(snapshot, hits);
  const st = await uploadFilesToNetdisk([{ name: 'a.txt', size: 10 }], deps);
  assert.deepEqual(transfers.map((b) => b.resource_ids), [[2]]);
  assert.equal(st.transferred, 1);
});
