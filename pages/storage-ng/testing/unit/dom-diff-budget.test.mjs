/**
 * Unit tests: keyed-diff frame budget + minimal reorder (FE-16).
 *
 * L10: the frame budget is enforced against *measured* DOM writes. It used to
 * charge the whole-list fragment append its row count (so normal renders kept
 * tripping the gate), then swung to charging it 1 unconditionally (so a
 * reorder hid 100 DOM moves behind a single unit). Reorders now go through the
 * LIS minimal-move pass: each move is one honest write, small reorders cost a
 * couple of units, and even a full reversal is chunked instead of overrunning
 * a frame. The runSeq supersede semantics must stay exactly as they were.
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
    insertBefore(n, ref) {
      if (n.parentNode) {
        const prev = n.parentNode.children.indexOf(n);
        if (prev > -1) n.parentNode.children.splice(prev, 1);
      }
      const at = ref === null || ref === undefined
        ? this.children.length : this.children.indexOf(ref);
      if (at === -1) throw new Error('insertBefore: reference is not a child');
      this.children.splice(at, 0, n);
      n.parentNode = this;
      n.isConnected = true;
      return n;
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
  const res = applyKeyedDiff(tbody, [...many, { id: 'extra', name: 'b' }], render, (x) => x.id);
  await drain();
  const after = getDiffStats();
  assert.deepEqual(keys(tbody), [...many.map((m) => m.id), 'extra']);
  assert.equal(res.moves, 1,
    `appending one row must cost one DOM move, not one per row (got ${res.moves})`);
  assert.equal(after.violations, before.violations,
    `a normal append must not be reported as a budget violation (${after.violations} > ${before.violations})`);
  assert.ok(after.maxFrameWrites <= MAX_ROWS_PER_FRAME,
    `maxFrameWrites ${after.maxFrameWrites} must stay within ${MAX_ROWS_PER_FRAME}`);
  assert.ok(after.maxMoves >= 1, 'the DOM move count must be observable');
});

test('L10: reordering a long list is chunked and stays within budget', async () => {
  const tbody = el('tbody');
  const many = list(MAX_ROWS_PER_FRAME + 10);
  applyKeyedDiff(tbody, many, render, (x) => x.id);
  await drain();
  const before = getDiffStats();
  const reversed = [...many].reverse();
  const res = applyKeyedDiff(tbody, reversed, render, (x) => x.id);
  await drain();
  const after = getDiffStats();
  assert.deepEqual(keys(tbody), reversed.map((m) => m.id), 'the reorder must still land in want order');
  assert.equal(after.violations, before.violations,
    `moves must be chunked across frames, not overrun one frame (${after.violations} > ${before.violations})`);
  assert.ok(after.maxFrameWrites <= MAX_ROWS_PER_FRAME,
    `maxFrameWrites ${after.maxFrameWrites} must stay within ${MAX_ROWS_PER_FRAME}`);
  // A full reversal has an increasing run of length 1, so N-1 moves is the
  // provable minimum; anything larger means the LIS pass regressed.
  assert.equal(res.moves, many.length - 1,
    `a reversal must plan the minimum move set (N-1 = ${many.length - 1}, got ${res.moves})`);
});

test('LIS: moving one row to the tail costs one move, not N', async () => {
  const tbody = el('tbody');
  const rows = list(5);
  applyKeyedDiff(tbody, rows, render, (x) => x.id);
  await drain();
  // Old full-reappend pass: 5 moves. Minimal: 1 (only r0 leaves its slot).
  const moved = [...rows.slice(1), rows[0]];
  const res = applyKeyedDiff(tbody, moved, render, (x) => x.id);
  await drain();
  assert.deepEqual(keys(tbody), moved.map((m) => m.id));
  assert.equal(res.moves, 1,
    `shifting one row past an ordered run must move only that row (got ${res.moves})`);
});

test('LIS: an unchanged order with data edits plans zero moves', async () => {
  const tbody = el('tbody');
  const rows = list(4);
  applyKeyedDiff(tbody, rows, render, (x) => x.id);
  await drain();
  const edited = rows.map((r, i) => ({ ...r, name: i === 0 ? 'changed' : 'a' }));
  const res = applyKeyedDiff(tbody, edited, render, (x) => x.id);
  await drain();
  assert.equal(res.moves, 0, 'in-place replacement needs no move');
  // rewrittenRows only accumulates while the frame ops run, so the live value
  // is the stats snapshot rather than the synchronous return.
  assert.equal(getDiffStats().lastRewrittenRows, 1);
  assert.deepEqual(keys(tbody), rows.map((r) => r.id));
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
