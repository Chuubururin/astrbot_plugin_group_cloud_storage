/**
 * Unit tests: the task panel renders through the shared keyed diff.
 *
 * The panel used to hand-roll its own keyed-row pass: an `existing` Map built
 * from list.children, a want-set removal sweep, and an unconditional updateRow
 * per model row. That second implementation had two defects this file pins
 * down, plus the one behavior change merging it brought in:
 *
 *  - pushTaskLog composes log_id from `${ts}-${task_id}-${type}`, so two events
 *    in the same millisecond for the same task and type collide. The hand-rolled
 *    Map held one node per key, so a row silently disappeared; applyKeyedDiff
 *    disambiguates duplicates with a \u0000dup<n> suffix and warns.
 *  - an unchanged row was rewritten on every store notification (updateRow ran
 *    unconditionally); the render signature now lets an identical projection
 *    skip the row entirely, node and all.
 *  - rendering happens inside requestAnimationFrame and coalesces, so every
 *    assertion here runs against a flushed frame (see drain()), and one test
 *    asserts the un-flushed state on purpose.
 *
 * Run: node --test pages/storage-ng/testing/unit/task-panel.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

// ---- minimal DOM stub (functional children: the diff really mutates it) ----
const byId = new Map();

function el(tag = 'div') {
  return {
    tagName: String(tag).toUpperCase(),
    dataset: {}, children: [], style: {}, className: '',
    value: '', disabled: false, isConnected: false, parentNode: null,
    _id: undefined,
    classList: {
      _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); },
      contains(c) { return this._s.has(c); },
    },
    get id() { return this._id; },
    set id(v) { this._id = v; byId.set(v, this); },
    get textContent() { return this._text || ''; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() {
      if (this._html !== undefined) return this._html;
      // Browser serialization: text set through textContent comes back with the
      // markup characters escaped (see escape.test.mjs SpanShim). utils/helpers
      // escapeHtml depends on exactly this round-trip.
      if (this._text !== undefined) {
        return this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }
      return '';
    },
    set innerHTML(v) { this._html = v; },
    appendChild(c) {
      if (c.tagName === 'FRAGMENT') {
        for (const k of [...c.children]) this.appendChild(k);
        c.children = [];
        return;
      }
      if (c.parentNode) {
        const i = c.parentNode.children.indexOf(c);
        if (i > -1) c.parentNode.children.splice(i, 1);
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
    addEventListener() {}, click() {}, setAttribute() {},
    // The panel looks its nodes up by id, as the real document does. This stub
    // keeps the template string it writes as an opaque string, so the lookup
    // resolves against the same byId map the fixture below fills.
    querySelector(sel) { return sel.startsWith('#') ? byId.get(sel.slice(1)) || null : null; },
    querySelectorAll() { return []; },
  };
}

globalThis.document = {
  body: el('body'),
  createElement: (t) => el(t),
  createDocumentFragment: () => el('fragment'),
  getElementById: (id) => byId.get(id) || null,
  addEventListener() {},
  querySelector: () => null,
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
globalThis.window = { location: { search: '' }, addEventListener() {} };

const store = await import('../../store.js');
const { getDiffStats } = await import('../../utils/dom-diff.js');
const { initTaskPanel } = await import('../../components/task-panel.js');

// initTaskPanel fills the panel head/list through innerHTML, which this stub
// does not parse: register the two nodes it queries by id as the fixture.
const list = el('div');
byId.set('task-panel-list', list);
const count = el('span');
byId.set('task-panel-count', count);

initTaskPanel();

/** Let every queued rAF - and the frames it chains - run. */
async function drain(turns = 10) {
  for (let i = 0; i < turns; i += 1) await new Promise((r) => setTimeout(r, 0));
}

const rowKeys = () => list.children.map((c) => c.dataset.key);
/** The `.task-detail` text the row builder inlined. */
const detailOf = (node) => node.innerHTML.match(/class="task-detail">([^<]*)</)?.[1] || '';

/** Start from an empty, settled panel. */
async function reset() {
  store.clearTaskLog();
  await drain();
  assert.deepEqual(rowKeys(), [], 'clearing the log must release every row');
}

/** One SSE task event, in the shape main.js hands to pushTaskLog. */
const ev = (over = {}) => ({
  type: 'progress', task_id: 't1', kind: 'upload', detail: '上传中',
  ts: 1700000000000, ...over,
});

/**
 * Push one event and return the console warnings its render planned.
 *
 * The duplicate-key warning fires while applyKeyedDiff builds the plan (i.e.
 * synchronously inside pushTaskLog), so swallowing it here covers the frame.
 * Tests that do not assert the text stay quiet in the TAP output.
 */
function push(evInput) {
  const real = console.warn;
  const seen = [];
  console.warn = (...args) => { seen.push(args.join(' ')); };
  try {
    store.pushTaskLog(evInput);
  } finally {
    console.warn = real;
  }
  return seen;
}

test('clearing the log releases every row', async () => {
  await reset();
  push(ev());
  await drain();
  assert.equal(list.children.length, 1, 'one event, one row');
  await reset();
});

test('duplicate log_id keeps both rows and warns (the hand-rolled pass lost one)', async () => {
  await reset();
  // Same millisecond + same task + same type -> the same log_id in store.js.
  const warnings = [];
  warnings.push(...push(ev({ percent: 20 })));
  await drain();
  warnings.push(...push(ev({ percent: 60 })));
  await drain();

  assert.equal(list.children.length, 2,
    `a colliding log_id must not swallow a row (got [${rowKeys()}])`);
  assert.deepEqual(rowKeys(), [
    '1700000000000-t1-progress',
    '1700000000000-t1-progress\u0000dup2',
  ], 'the second row carries the disambiguated key');
  assert.ok(warnings.some((w) => /duplicate row keys/i.test(w)),
    `the collision must be reported, got: ${JSON.stringify(warnings)}`);
  assert.deepEqual(list.children.map(detailOf), ['上传中 60%', '上传中 20%'],
    'newest first, and each row shows its own event');
});

test('progress update lands the new detail text on the existing key', async () => {
  await reset();
  push(ev({ percent: 20 }));
  await drain();
  const before = list.children[0];
  assert.equal(detailOf(before), '上传中 20%');

  // Same ts/task/type (-> same log_id) with a new percent: the keyed diff
  // replaces the row instead of patching it in place.
  push(ev({ percent: 85 }));
  await drain();
  const after = list.children[0];
  assert.equal(detailOf(after), '上传中 85%', 'the new percent must reach the DOM');
  assert.notEqual(after, before,
    'a changed row is rebuilt by buildRow (the accepted trade for in-place updateRow)');
  assert.notEqual(list.children[1], before, 'the old node left the tree');
  assert.equal(detailOf(list.children[1]), '上传中 20%',
    'the superseded event keeps its own (dup-keyed) row');
});

test('an identical render signature leaves the row node untouched', async () => {
  await reset();
  push(ev({ percent: 40 }));
  await drain();
  const node = list.children[0];

  // A *different* DTO object that projects to the same markup: the signature
  // says unchanged, so the node must survive with its identity intact - the old
  // pass called updateRow on it regardless.
  push(ev({ percent: 40 }));
  await drain();
  assert.equal(list.children[0], node, 'unchanged row node must be reused, not rebuilt');
  assert.equal(list.children.length, 2, 'the duplicate event still gets its own row');
  assert.equal(getDiffStats().lastRewrittenRows, 1,
    'only the new row is written; the existing one is untouched');
});

test('a field the row does not render cannot rewrite it (signature, not whole-DTO compare)', async () => {
  await reset();
  push(ev({ percent: 40 }));
  await drain();
  const node = list.children[0];

  // The same log entry as a re-delivery would carry it: a fresh object whose
  // only difference is an unrendered OpQueue counter (`i` drives the header
  // queue indicator, never the panel text). Without a signatureFn the diff
  // compares the whole DTO and would rebuild this row.
  store.set('taskLog', [{ ...store.getState().taskLog[0], i: 7 }]);
  await drain();
  assert.equal(list.children[0], node, 'an unrendered field must not rewrite the row');
  assert.equal(getDiffStats().lastRewrittenRows, 0, 'the frame wrote no row at all');
});

test('rendering is deferred one frame and coalesced; the count is not', async () => {
  await reset();
  push(ev({ ts: 1700000000001 }));
  push(ev({ ts: 1700000000002 }));
  push(ev({ ts: 1700000000003 }));

  assert.equal(list.children.length, 0,
    'applyKeyedDiff mutates inside rAF: nothing may be on screen before the frame');
  assert.equal(count.textContent, '3 条',
    'the head count is a single text write and stays synchronous');

  await drain();
  assert.deepEqual(rowKeys(), [
    '1700000000003-t1-progress',
    '1700000000002-t1-progress',
    '1700000000001-t1-progress',
  ], 'the newest event leads, and the two superseded renders lost nothing');
});
