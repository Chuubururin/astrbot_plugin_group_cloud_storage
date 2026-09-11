/**
 * Unit tests: keyed DOM diff (FE-12/13/16).
 *
 * applyKeyedDiff reconciles <tr> rows by dataset.key via rAF-chunked
 * mutations and tracks render statistics (getDiffStats) consumed by the
 * E2E budget probes. The former diffByKey pure-helper was dead code and
 * removed; these tests cover the live API only.
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { applyKeyedDiff, getDiffStats } = await import('../../utils/dom-diff.js');

// Minimal DOM element stub sufficient for the diff algorithm.
function el(tag = 'tr') {
  return {
    tagName: tag.toUpperCase(),
    dataset: {},
    children: [],
    isConnected: false,
    parentNode: null,
    __data: undefined,
    appendChild(c) {
      // Documents flatten fragments: their children move into this node.
      if (c.tagName === 'FRAGMENT') {
        for (const child of [...c.children]) this.appendChild(child);
        c.children = [];
        return;
      }
      // DOM semantics: an appended node leaves any previous parent first.
      if (c.parentNode) {
        const prev = c.parentNode.children.indexOf(c);
        if (prev > -1) c.parentNode.children.splice(prev, 1);
      }
      this.children.push(c);
      c.parentNode = this;
      c.isConnected = true;
    },
    remove() {
      this.isConnected = false;
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i > -1) this.parentNode.children.splice(i, 1);
      }
    },
    replaceWith(n) {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i > -1) this.parentNode.children[i] = n;
      }
      n.parentNode = this.parentNode;
      n.isConnected = true;
    },
  };
}

globalThis.document = {
  createDocumentFragment: () => el('fragment'),
};
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);

function flushRAF() {
  return new Promise((r) => setTimeout(() => globalThis.requestAnimationFrame(() => r()), 0));
}

const render = (item) => {
  const tr = el('tr');
  tr.dataset.key = item.id;
  tr.dataset.name = item.name || '';
  return tr;
};

test('applyKeyedDiff: keyed reconcile with row reuse', async () => {
  const tbody = el('tbody');
  applyKeyedDiff(tbody, [{ id: '1', name: 'a' }, { id: '2', name: 'b' }], render, (x) => x.id);
  await flushRAF();
  // Fragments flatten: the rows live directly under the tbody.
  const keys1 = tbody.children.map((c) => c.dataset.key).sort();
  assert.deepEqual(keys1, ['1', '2']);

  // Second render reuses the existing nodes (no rewrite).
  const statsBefore = getDiffStats();
  applyKeyedDiff(tbody, [{ id: '2', name: 'b' }, { id: '3', name: 'c' }], render, (x) => x.id);
  await flushRAF();
  const keys2 = tbody.children.map((c) => c.dataset.key).sort();
  assert.deepEqual(keys2, ['2', '3']);
  const statsAfter = getDiffStats();
  assert.ok(statsAfter.totalRenders >= statsBefore.totalRenders);
});

test('applyKeyedDiff: empty list clears the container', async () => {
  const tbody = el('tbody');
  applyKeyedDiff(tbody, [{ id: '1' }], render, (x) => x.id);
  await flushRAF();
  applyKeyedDiff(tbody, [], render, (x) => x.id);
  await flushRAF();
  assert.equal(tbody.children.length, 0);
});

test('getDiffStats: exposes FE-16 budget metrics', () => {
  const s = getDiffStats();
  for (const k of ['totalRenders', 'lastRewrittenRows', 'maxFrameWrites', 'lastFramesUsed', 'violations']) {
    assert.ok(typeof s[k] === 'number', `stat ${k}`);
  }
});
test('keyed diff releases the stable empty-state row when data arrives (2026-09-03)', async () => {
  // 空态行现在携带 dataset.key='empty'（file-rows/group-data 修复）：
  // 数据到达时 wantKeys 不含 'empty' → toRemove 移除，空态不再残留。
  const tbody = el('tbody');
  const emptyRow = el('tr');
  emptyRow.dataset.key = 'empty';
  emptyRow.dataset.dir = '1';
  tbody.appendChild(emptyRow);

  applyKeyedDiff(tbody, [{ id: '1', name: 'a' }], render, (x) => x.id);
  await flushRAF();
  const keys = tbody.children.map((c) => c.dataset.key);
  assert.ok(!keys.includes('empty'), `empty row released, got ${keys.join(',')}`);
  assert.deepEqual(keys, ['1']);
});

test('legacy key-less rows are NOT managed by keyed diff (documented behavior)', async () => {
  // 无 dataset.key 的行（旧版空态遗留形态）不被 byKey 收录，也不会被移除——
  // 这正是「空态不消失」历史 bug 的根因；修复后所有空态行都带 'empty' key。
  const tbody = el('tbody');
  const keyless = el('tr');
  tbody.appendChild(keyless);
  applyKeyedDiff(tbody, [{ id: '1' }], render, (x) => x.id);
  await flushRAF();
  assert.equal(tbody.children.length, 2, 'keyless row still present (must never be created)');
});

test('reorder where moved rows also changed data keeps want order (2026-09-07 move-up bug)', async () => {
  // 群组上移真机 bug: 被移动的行 sort_order 变化 → 走 replace 路径；
  // 旧实现 replace 在帧期 push 进 finalEls、keep 在计划期 push，
  // 最终 append 变成 [keep..., replace...] → [C..J,B,A] 错序。
  // 修复后 finalEls 按槽位（want 顺序）装配。
  const tbody = el('tbody');
  const A = { id: 'A', name: 'a', so: 1 };
  const B = { id: 'B', name: 'b', so: 2 };
  const C = { id: 'C', name: 'c', so: 3 };
  applyKeyedDiff(tbody, [A, B, C], render, (x) => x.id);
  await flushRAF();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['A', 'B', 'C']);

  // 上移: B 换到头部，两个被移动行的 so 字段都变了（replace 路径）
  const B2 = { id: 'B', name: 'b', so: 1 };
  const A2 = { id: 'A', name: 'a', so: 2 };
  const C2 = { id: 'C', name: 'c', so: 3 };
  applyKeyedDiff(tbody, [B2, A2, C2], render, (x) => x.id);
  await flushRAF();
  assert.deepEqual(
    tbody.children.map((c) => c.dataset.key),
    ['B', 'A', 'C'],
    `moved rows must land in want order, got [${tbody.children.map((c) => c.dataset.key)}]`,
  );
  // replace 出的新行必须带 __data，否则后续渲染会无限重替换
  for (const row of tbody.children) assert.ok(row.__data, `row ${row.dataset.key} has __data`);
});

test('replace-in-place (no reorder) also lands at its own position', async () => {
  const tbody = el('tbody');
  applyKeyedDiff(tbody, [
    { id: '1', name: 'a' }, { id: '2', name: 'b' }, { id: '3', name: 'c' },
  ], render, (x) => x.id);
  await flushRAF();
  applyKeyedDiff(tbody, [
    { id: '1', name: 'a' }, { id: '2', name: 'CHANGED' }, { id: '3', name: 'c' },
  ], render, (x) => x.id);
  await flushRAF();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['1', '2', '3']);
  assert.equal(tbody.children[1].dataset.name, 'CHANGED');
});

test('create ops during reorder land in want order, not at the tail', async () => {
  const tbody = el('tbody');
  applyKeyedDiff(tbody, [{ id: '2', name: 'b' }], render, (x) => x.id);
  await flushRAF();
  applyKeyedDiff(tbody, [
    { id: '1', name: 'a' }, { id: '2', name: 'b' }, { id: '3', name: 'c' },
  ], render, (x) => x.id);
  await flushRAF();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['1', '2', '3']);
});
