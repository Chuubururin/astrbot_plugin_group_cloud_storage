/**
 * Unit tests: download executor (features/download.js) — one code path
 * with internal target subdivision; 转存网盘 = target 'netdisk' (batched
 * bridge/transfer per group).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { API } from '../../api.js';
import { downloadItems, DOWNLOAD_TARGET_OPTIONS } from '../../features/download.js';
import { runEachWithFailures } from '../../utils/helpers.js';

// Minimal DOM stubs (executor imports modal/toast chains indirectly).
const makeEl = () => ({
  className: '', classList: { add() {}, remove() {}, toggle() {} },
  dataset: {}, children: [], style: {}, innerHTML: '', textContent: '',
  appendChild() {}, remove() {}, addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, setAttribute() {}, click() {}, select() {},
});
globalThis.document = {
  body: makeEl(),
  createElement: () => makeEl(),
  getElementById: () => null,
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll: () => [],
};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
// Node 22 exposes navigator as getter-only; clipboard stubbing is
// unnecessary because the executor receives `copy` via deps.

const STATE = { currentGroup: '10001' };

function rows(...spec) {
  return spec.map(([id, group, extra]) => ({ id, group_id: group, name: `f${id}`, ...extra }));
}

test('download: netdisk target batches resource_ids per group (转存=下载特殊形式)', async () => {
  const calls = [];
  const deps = {
    apiPost: async (p, body) => {
      calls.push([p, body]);
      return { results: (body.resource_ids || []).map((id) => ({ resource_id: id, task_id: `t${id}` })), errors: [] };
    },
  };
  const ctx = { rows: rows([1, 'g1'], [2, 'g1'], [3, 'g2']), state: STATE };
  const r = await downloadItems(ctx, 'netdisk', { deps });
  assert.equal(calls.length, 2, 'one bridge/transfer call per group');
  assert.equal(calls[0][0], API.BRIDGE.TRANSFER);
  assert.deepEqual(calls[0][1].resource_ids, [1, 2]);
  assert.equal(calls[0][1].group, 'g1');
  assert.deepEqual(calls[1][1].resource_ids, [3]);
  assert.equal(r.done, 3);
  assert.deepEqual(r.failed, []);
  assert.equal(r.ok, true);
});

test('download: netdisk target surfaces per-item errors without throwing', async () => {
  const deps = {
    apiPost: async () => ({ results: [], errors: ['resource_id=9: not managed'] }),
  };
  const ctx = { rows: rows([9, 'g1']), state: STATE };
  const r = await downloadItems(ctx, 'netdisk', { deps });
  assert.equal(r.ok, false);
  assert.equal(r.done, 0);
  assert.equal(r.failed.length, 1);
});

test('download: local target downloads per row and collects failures', async () => {
  const downloaded = [];
  let n = 0;
  const deps = {
    download: async (path, params, name) => {
      if (++n === 2) throw new Error('boom');
      downloaded.push([path, params, name]);
    },
  };
  const ctx = { rows: rows([1, 'g1'], [2, 'g1'], [3, 'g1']), state: STATE };
  const r = await downloadItems(ctx, 'local', { deps });
  assert.equal(downloaded.length, 2, 'no early stop');
  assert.equal(downloaded[0][0], API.FILES.DOWNLOAD);
  assert.equal(downloaded[0][1].group, 'g1');
  assert.equal(r.done, 2);
  assert.equal(r.failed.length, 1);
  assert.match(r.failed[0], /f2: boom/);
  assert.equal(r.ok, false);
});

test('download: local target volume confirm gates the whole batch', async () => {
  let asked = null;
  const deps = { download: async () => {} };
  const ctx = { rows: rows([1, 'g1', { is_volume: true }], [2, 'g1']), state: STATE };
  const r1 = await downloadItems(ctx, 'local', {
    deps, confirmVolumes: async (n) => { asked = n; return false; },
  });
  assert.equal(asked, 1);
  assert.equal(r1.cancelled, true);
  const r2 = await downloadItems(ctx, 'local', {
    deps, confirmVolumes: async () => true,
  });
  assert.equal(r2.done, 2);
  assert.equal(r2.cancelled, undefined);
});

test('download: incomplete volumes skip the confirm and carry allow_incomplete', async () => {
  const downloaded = [];
  let asked = 0;
  const deps = { download: async (p, params) => downloaded.push(params) };
  const ctx = {
    rows: rows(
      [1, 'g1', { is_volume: true, volume_total: 4, volume_done: 3, volume_complete: false }],
      [2, 'g1', { is_volume: true, volume_total: 2, volume_done: 2, volume_complete: true }],
    ),
    state: STATE,
  };
  const r = await downloadItems(ctx, 'local', {
    deps, confirmVolumes: async () => { asked++; return true; },
  });
  assert.equal(asked, 1, 'only the complete volume triggers the confirm');
  assert.equal(r.ok, true);
  assert.equal(downloaded[0].allow_incomplete, 1, 'missing-part volume downloads degraded');
  assert.equal(downloaded[1].allow_incomplete, undefined, 'complete volume stays strict');
});

test('download: canonical target table (shared with distribute commands)', async () => {
  const { DOWNLOAD_TARGETS, targetOptions, targetLabel } = await import(
    '../../features/download-targets.js');
  assert.deepEqual(
    DOWNLOAD_TARGETS.map((t) => t.value),
    ['local', 'link', 'address', 'netdisk', 'album', 'essence', 'group', 'copy'],
  );
  // Filtering keeps canonical order and labels stay user-facing.
  assert.deepEqual(
    targetOptions(['local', 'group']).map((t) => t.value),
    ['local', 'group'],
  );
  assert.equal(targetLabel('netdisk'), '转存到网盘');
  assert.equal(targetOptions().length, DOWNLOAD_TARGETS.length, 'no filter = full table');
});

test('download: link target batches files/links and copies joined urls', async () => {
  const copied = [];
  const deps = {
    apiPost: async (p, body) => {
      assert.equal(p, API.FILES.LINKS);
      assert.deepEqual(body.items, [
        { id: 1, group: 'g1' }, { id: 2, group: 'g2' }, { id: 3, group: 'g1' },
      ]);
      return { links: [{ id: 1, url: 'u1' }, { id: 2, url: 'u2' }], errors: ['id=3: offline'] };
    },
    copy: async (t) => { copied.push(t); },
  };
  const ctx = { rows: rows([1, 'g1'], [2, 'g2'], [3, 'g1']), state: STATE };
  const r = await downloadItems(ctx, 'link', { deps });
  assert.deepEqual(copied, ['u1\nu2']);
  assert.equal(r.done, 2);
  assert.deepEqual(r.failed, ['id=3: offline']);
  assert.equal(r.copied, 'u1\nu2');
});

test('download: link target refuses >20 rows', async () => {
  const deps = { apiPost: async () => { throw new Error('should not be called'); } };
  const ctx = { rows: Array.from({ length: 21 }, (_, i) => ({ id: i, group_id: 'g1', name: `f${i}` })), state: STATE };
  const r = await downloadItems(ctx, 'link', { deps });
  assert.equal(r.ok, false);
  assert.match(r.failed[0], /最多 20 项/);
});

test('download: album/essence target distributes per row, volume rows fail explicitly', async () => {
  const posts = [];
  const deps = {
    apiPost: async (p, body) => {
      posts.push([p, body]);
      return { target: body.target, task_id: 'x' };
    },
  };
  const ctx = { rows: rows([1, 'g1'], [2, 'g1', { is_volume: true }]), state: STATE };
  const r = await downloadItems(ctx, 'album', { deps });
  assert.equal(posts.length, 1, 'volume row skipped with explicit failure');
  assert.equal(posts[0][0], API.FILES.DISTRIBUTE);
  assert.equal(posts[0][1].target, 'album');
  assert.equal(r.done, 1);
  assert.equal(r.failed.length, 1);
  assert.match(r.failed[0], /分卷资源请使用「下载」/);
});

test('download: address target requires a single row', async () => {
  const deps = { apiGet: async () => ({ http_url: 'http://x' }) };
  const multi = await downloadItems({ rows: rows([1, 'g1'], [2, 'g1']), state: STATE }, 'address', { deps });
  assert.equal(multi.ok, false);
  const one = await downloadItems({ rows: rows([1, 'g1']), state: STATE }, 'address', { deps });
  assert.equal(one.ok, true);
  assert.equal(one.address.http_url, 'http://x');
});

test('download: unknown target throws (integration error, not silent)', async () => {
  await assert.rejects(() => downloadItems({ rows: [], state: STATE }, 'wat', {}));
});

test('download: target option table covers the canonical forms', () => {
  assert.deepEqual(
    DOWNLOAD_TARGET_OPTIONS.map((t) => t.value),
    ['local', 'link', 'address', 'netdisk', 'album', 'essence', 'group', 'copy'],
  );
});

test('download: promptDownloadTarget offers only implemented targets', async () => {
  // 缺陷回归: files-distribute 曾渲染全量 8 项, 但 downloadItems 未实现
  // group/copy -> "unknown download target"。模态必须只给执行器支持的子集。
  // ES module 导出只读, 这里直接断言 targetOptions 的过滤契约。
  const { targetOptions } = await import('../../features/download-targets.js');
  const values = targetOptions(['local', 'link', 'address', 'netdisk', 'album', 'essence'])
    .map((t) => t.value);
  assert.deepEqual(values, ['local', 'link', 'address', 'netdisk', 'album', 'essence']);
  assert.ok(!values.includes('group') && !values.includes('copy'),
    'unimplemented targets must not be offered');
});

test('runEachWithFailures: labels failures with item name/id, keeps going', async () => {
  const { done, failed } = await runEachWithFailures(
    [{ name: 'a' }, { id: 7 }, { name: 'c' }],
    async (it, i) => { if (i !== 1) throw new Error(`bad ${it.name || it.id}`); },
  );
  assert.equal(done, 1);
  assert.equal(failed.length, 2);
  assert.match(failed[0], /^a: bad a$/);
  assert.match(failed[1], /^c: bad c$/);
});
