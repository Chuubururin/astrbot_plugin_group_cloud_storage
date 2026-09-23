/**
 * Unit tests: keyed-diff frame budget + minimal reorder.
 *
 * The frame budget is charged in weighted units, one unit per row rebuild and
 * MOVE_UNITS per reparent move, and reorders go through the LIS minimal-move
 * pass. The runSeq supersede semantics are pinned here too.
 *
 * What these assertions can and cannot detect: the dearest single op is a row
 * replacement at 2 units, far under the 50-unit budget, so no render can
 * overrun a frame and `violations` cannot fire on today's code. It stays
 * asserted as a canary - it wakes up the moment anyone batches writes back
 * into one op. The live signal is `moves` on the return value, which pins the
 * move set to the LIS minimum.
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
        // A real detached node stops reporting its old parent; without this
        // the stub lets parentNode lie, and the diff's "is my anchor still in
        // the container" guard sees a node that children no longer holds.
        this.parentNode = null;
      }
    },
    replaceWith(n) {
      const parent = this.parentNode;
      if (!parent) return;       // as in the DOM: a detached node's replaceWith
                                 // is a no-op, so the fresh row never lands
      const i = parent.children.indexOf(this);
      if (i > -1) parent.children[i] = n;
      n.parentNode = parent;
      n.isConnected = true;
      this.parentNode = null;    // the replaced node leaves the tree
      this.isConnected = false;
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

test('L10: a whole-page reorder settles in one frame', async () => {
  const tbody = el('tbody');
  const many = list(MAX_ROWS_PER_FRAME * 2);
  applyKeyedDiff(tbody, many, render, (x) => x.id);
  await drain();
  const before = getDiffStats();
  const reversed = [...many].reverse();
  const res = applyKeyedDiff(tbody, reversed, render, (x) => x.id);
  await drain();
  const after = getDiffStats();
  assert.deepEqual(keys(tbody), reversed.map((m) => m.id));
  // A reparent is pointer surgery, not content construction, so it is charged
  // a fraction of a row rebuild: 2N moves must still fit the frame that a
  // single N-row create pass needs two frames for.
  assert.equal(after.lastFramesUsed, 1,
    `a pure reorder must settle in one frame, took ${after.lastFramesUsed}`);
  assert.equal(after.violations, before.violations);
  // The charge is weighted; the real move count must stay observable.
  assert.equal(res.moves, many.length - 1,
    `moves must still report every DOM move (got ${res.moves})`);
  assert.ok(after.maxMoves >= many.length - 1,
    'maxMoves must still expose the true move count');
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

// ---- W: the plan survives an external write between frames -----------------
// renderRows() (empty state, pane switches), renderInto() with an empty slice,
// renderErrorRow() and the retry row all write these containers directly, and
// none of them bumps runSeq - so they can land *inside* a chunked render.
// Before the anchor/parent guards, the first such hit made insertBefore throw
// NotFoundError inside runChunk: the remaining ops never ran, the list stayed
// half-built, and the render never reported as settled.

/** Step exactly one animation frame (the rAF stub is a setTimeout). */
const oneFrame = () => new Promise((r) => setTimeout(r, 0));
const detachAll = (tbody) => {
  for (const row of [...tbody.children]) row.remove();
  tbody.children.length = 0;
};
const sig = (r) => r.name;

test('a wipe between frames settles the plan instead of aborting it', async () => {
  const tbody = el('tbody');
  // 250 rows: a full reversal plans 249 moves = 62.25 units, so the plan is
  // still running when the second frame starts (one frame holds 200 moves).
  const rows = list(250);
  applyKeyedDiff(tbody, rows, render, (x) => x.id, sig);
  await drain(40);
  const reversed = [...rows].reverse();
  const before = getDiffStats();

  applyKeyedDiff(tbody, reversed, render, (x) => x.id, sig);
  await oneFrame();                       // part of the move set is applied
  detachAll(tbody);                       // what renderErrorRow() does
  await drain(40);

  const after = getDiffStats();
  assert.ok(after.totalRenders > before.totalRenders,
    'the run must reach its settle branch (an escaped error skips it)');
  assert.ok(after.violations === before.violations, 'and it must not overrun a frame');

  applyKeyedDiff(tbody, reversed, render, (x) => x.id, sig);
  await drain(40);
  assert.deepEqual(keys(tbody), reversed.map((r) => r.id),
    'the next render must rebuild the full list in the wanted order');
});

test('a replace whose row was detached externally re-enters the DOM', async () => {
  const tbody = el('tbody');
  const rows = list(3);
  applyKeyedDiff(tbody, rows, render, (x) => x.id, sig);
  await drain();

  // Change only r1: a replace op is planned, and (since the order still
  // matches) no move op exists to rescue a row the replace loses.
  const next = rows.map((r) => (r.id === 'r1' ? { ...r, name: 'b' } : r));
  applyKeyedDiff(tbody, next, render, (x) => x.id, sig);
  tbody.children.find((c) => c.dataset.key === 'r1').remove();
  await drain();

  assert.deepEqual(keys(tbody).sort(), ['r0', 'r1', 'r2'],
    'replaceWith() on a detached node is a no-op - the row must be appended instead');
  const back = tbody.children.find((c) => c.dataset.key === 'r1');
  assert.equal(back.dataset.name, 'b', 'and it carries the new data');
  assert.notEqual(keys(tbody)[1], 'r1',
    'the tail is where it lands: this plan had no move op (order matched at plan time)');

  applyKeyedDiff(tbody, next, render, (x) => x.id, sig);
  await drain();
  assert.deepEqual(keys(tbody), ['r0', 'r1', 'r2'],
    'the next render re-plans from the live DOM and restores the order');
});

// ---- mutations (real writes) vs. weighted frame units --------------------
test('mutations count container writes, not weighted frame units', async () => {
  const tbody = el('tbody');
  const rows = list(4);
  const m0 = getDiffStats().mutations;
  applyKeyedDiff(tbody, rows, render, (x) => x.id, sig);
  await drain();
  const first = getDiffStats();
  assert.equal(first.mutations - m0, 4,
    'four created rows are four writes (a create op builds a detached row)');
  assert.equal(first.lastMutations, 4);

  const second = getDiffStats();
  applyKeyedDiff(tbody, [...rows].reverse(), render, (x) => x.id, sig);
  await drain();
  const after = getDiffStats();
  assert.equal(after.mutations - second.mutations, 3,
    'a 4-row reversal is 3 LIS moves = 3 DOM writes');
  assert.equal(after.lastFramesUsed, 1,
    'one frame did all three writes while the weighted cost was only 0.75 units');
});

test('mutations per operation kind, and the units that hide them', async () => {
  const cases = [
    ['append one row', (rows) => [...rows, { id: 'new', name: 'a' }], 1],
    // A replacement is one container write (replaceWith) even though it costs
    // 2 units - the row body is rebuilt as well.
    ['replace one row', (rows) => rows.map((r, i) => (i ? r : { ...r, name: 'z' })), 1],
    ['move one row to the tail', (rows) => [rows[1], rows[2], rows[3], rows[0]], 1],
    ['remove three rows', (rows) => rows.slice(3), 3],
    // Two new rows landing in the middle: both writes happen in their moves.
    ['create + reorder mixed', (rows) => [rows[0], { id: 'n1', name: 'a' },
      { id: 'n2', name: 'a' }, ...rows.slice(1)], 2],
  ];
  for (const [name, mutate, expected] of cases) {
    const tbody = el('tbody');
    const rows = list(4);
    applyKeyedDiff(tbody, rows, render, (x) => x.id, sig);
    await drain();
    const before = getDiffStats().mutations;
    applyKeyedDiff(tbody, mutate(rows), render, (x) => x.id, sig);
    await drain();
    const made = getDiffStats().mutations - before;
    assert.equal(made, expected,
      `${name}: expected exactly ${expected} container write(s), got ${made}`);
  }
});
