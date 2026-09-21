/**
 * Unit tests: keyed-diff frame budget (FE-16).
 *
 * L10: the single batched fragment insert was charged its row count, so any
 * append or reorder of a list longer than MAX_ROWS_PER_FRAME exceeded the
 * budget on its own and hit the run-anyway fallback: every such frame counted
 * a `violations` and printed a console warning, turning the gate metric into
 * constant noise. A fragment insert is one DOM write and is now charged 1,
 * with its row count reported separately as maxBulkRows. Frame pacing and the
 * runSeq supersede semantics must stay exactly as they were.
 *
 * Run: node --test pages/storage-ng/testing/unit/dom-diff-budget.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { MAX_ROWS_PER_FRAME } from '../../constants.js';

// Minimal DOM element stub sufficient for the diff algorithm.
function el(tag = 'tr') {
  return {
    tagName: tag.toUpperCase(),
    dataset: {}, children: [], isConnected: false, parentNode: null, __data: undefined,
    appendChild(c) {
      if (c.tagName === 'FRAGMENT') {
        for (const child of [...c.children]) this.appendChild(child);
        c.children = [];
        return;
      }
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

globalThis.document = { createDocumentFragment: () => el('fragment') };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);

const { applyKeyedDiff, getDiffStats } = await import('../../utils/dom-diff.js');

/** Let every queued rAF - and the frames they chain - run. */
async function drain(turns = 20) {
  for (let i = 0; i < turns; i += 1) await new Promise((r) => setTimeout(r, 0));
}

const render = (item) => {
  const tr = el('tr');
  tr.dataset.key = item.id;
  tr.dataset.name = item.name || '';
  return tr;
};
const keys = (tbody) => tbody.children.map((c) => c.dataset.key);
const list = (n) => Array.from({ length: n }, (_, i) => ({ id: `r${i}`, name: 'a' }));

test('L10: appending to a long list is one batched write, not the row count', async () => {
  const tbody = el('tbody');
  const many = list(MAX_ROWS_PER_FRAME + 10);
  const before = getDiffStats();
  applyKeyedDiff(tbody, many, render, (x) => x.id);
  await drain();
  applyKeyedDiff(tbody, [...many, { id: 'extra', name: 'b' }], render, (x) => x.id);
  await drain();
  const after = getDiffStats();
  assert.deepEqual(keys(tbody), [...many.map((m) => m.id), 'extra']);
  assert.equal(after.violations, before.violations,
    `a normal append must not be reported as a budget violation (${after.violations} > ${before.violations})`);
  assert.ok(after.maxFrameWrites <= MAX_ROWS_PER_FRAME,
    `maxFrameWrites ${after.maxFrameWrites} must stay within ${MAX_ROWS_PER_FRAME}`);
  assert.ok(after.maxBulkRows > MAX_ROWS_PER_FRAME,
    'the bulk insert size must still be observable');
});

test('L10: reordering a long list is one batched write', async () => {
  const tbody = el('tbody');
  const many = list(MAX_ROWS_PER_FRAME + 10);
  applyKeyedDiff(tbody, many, render, (x) => x.id);
  await drain();
  const before = getDiffStats();
  const reversed = [...many].reverse();
  applyKeyedDiff(tbody, reversed, render, (x) => x.id);
  await drain();
  const after = getDiffStats();
  assert.deepEqual(keys(tbody), reversed.map((m) => m.id), 'the reorder must still land in want order');
  assert.equal(after.violations, before.violations,
    `a pure reorder must not be reported as a budget violation (${after.violations} > ${before.violations})`);
});

test('L10: per-row chunking across frames is unchanged', async () => {
  const tbody = el('tbody');
  const many = list(MAX_ROWS_PER_FRAME * 2);
  applyKeyedDiff(tbody, many, render, (x) => x.id);
  await drain();
  const s = getDiffStats();
  assert.ok(s.lastFramesUsed >= 2, `creates must still be chunked, got ${s.lastFramesUsed} frame(s)`);
  assert.equal(s.lastRewrittenRows, MAX_ROWS_PER_FRAME * 2);
  assert.deepEqual(keys(tbody), many.map((m) => m.id));
});

test('L10: runSeq supersede semantics are unchanged', async () => {
  const tbody = el('tbody');
  applyKeyedDiff(tbody, list(MAX_ROWS_PER_FRAME * 2), render, (x) => x.id);
  applyKeyedDiff(tbody, [{ id: 'only' }], render, (x) => x.id);
  await drain();
  assert.deepEqual(keys(tbody), ['only'],
    'the superseded chunked render must not append its rows');
});
