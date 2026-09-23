/**
 * Unit tests: tasks view loading - reload coalescing + keyed empty state.
 *
 * M9: loadTasks() is fired from six entry points (three filter subscriptions,
 * paging, the refresh button, post-action reloads) plus the SSE queue events and
 * the reconnect full refresh. Each used to forward its own POST /tasks, so one
 * batch cancel (20 CANCELLED events) was 20 concurrent identical requests. The
 * reloads now go through createCoalescedLoader: one request in flight, the rest
 * absorbed into exactly one tail rerun, which re-reads getState() so it carries
 * the *current* filter/page rather than the values at trigger time.
 *
 * L12: the empty state wrote tbody.innerHTML directly, which never bumps the
 * keyed-diff run generation, so a non-empty render already queued in rAF still
 * appended its rows behind the "暂无任务" row.
 *
 * Run: node --test pages/storage-ng/testing/unit/tasks-load.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// ---- minimal DOM stub (functional children: the diff really mutates it) ----
function el(tag = 'div') {
  return {
    tagName: String(tag).toUpperCase(),
    dataset: {}, children: [], style: {}, className: '', textContent: '', value: '',
    disabled: false, isConnected: false, parentNode: null,
    classList: {
      _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); },
      contains(c) { return this._s.has(c); },
    },
    get innerHTML() { return this._html || ''; },
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
    addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, setAttribute() {},
  };
}

const byId = new Map();
globalThis.document = {
  body: el('body'),
  createElement: (t) => el(t),
  createDocumentFragment: () => el('fragment'),
  getElementById: (id) => { if (!byId.has(id)) byId.set(id, el('div')); return byId.get(id); },
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

/** In-flight ledger requests, resolved by the tests in any order. */
const pending = [];
globalThis.window = {
  AstrBotPluginPage: {
    apiGet: async () => ({}),
    apiPost: (path) => {
      if (path !== API.TASKS) return Promise.resolve({});
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    },
  },
};

const { initTasksView } = await import('../../views/tasks.js');
const { getState, refresh } = await import('../../store.js');
const { API } = await import('../../api.js');

const controls = new Map();
const container = el('div');
container.querySelector = (sel) => {
  if (!controls.has(sel)) controls.set(sel, el('div'));
  return controls.get(sel);
};

// Mounting fires the first ledger load, so pending[0] is already in flight.
initTasksView(container);
assert.equal(pending.length, 1, 'initTasksView must fire exactly one ledger load');

const tick = () => new Promise((r) => setTimeout(r, 10));

/** Trigger a load; with in-flight coalescing, a second call while the first
 *  is still pending will NOT create a new pending request — it marks dirty
 *  and reruns after the first completes.  Returns the new pending entry if
 *  one was created, or null if coalesced. */
function startLoad() {
  const n = pending.length;
  refresh('tasks');
  if (pending.length > n) return pending[n];
  return null; // coalesced — no new request fired
}

test('M9: in-flight coalescing prevents concurrent ledger requests', async () => {
  // Drain any initial loads from initTasksView
  while (pending.length) pending.shift().resolve({ tasks: [] });
  await tick(); await tick(); await tick();
  // The initial load completed — _tasksInFlight is false.
  // Fire first load: creates a pending request.
  const before = pending.length;
  const first = startLoad();
  assert.ok(first, 'first load must create a pending request');
  assert.equal(pending.length, before + 1, 'pending queue grew by one');
  // Fire second load while first is in flight: must be coalesced.
  const second = startLoad();
  assert.equal(second, null, 'second load must be coalesced');
  assert.equal(pending.length, before + 1, 'pending queue did NOT grow (coalesced)');
  // Resolve the first → dirty rerun fires.
  first.resolve({ tasks: [{ task_id: 'r1', state: 'pending' }] });
  await tick(); await tick(); await tick();
  // The rerun may or may not have appeared (depends on microtask ordering);
  // resolve anything that's pending so we don't leak.
  while (pending.length > before) pending.shift().resolve({ tasks: [{ task_id: 'r2', state: 'pending' }] });
  await tick();
  // Key invariant: the second call did NOT create a concurrent request.
  const ledger = getState().taskLedger;
  assert.ok(ledger.length > 0, 'taskLedger must have data');
});

test('L12: the empty state goes through the keyed diff', async () => {
  while (pending.length) pending.shift().resolve({ tasks: [] });
  await tick(); await tick(); await tick();
  const tbody = byId.get('task-tbody');
  tbody.children.length = 0;
  // Load non-empty data
  const first = startLoad();
  assert.ok(first);
  first.resolve({ tasks: [{ task_id: 't1', state: 'pending' }, { task_id: 't2', state: 'pending' }] });
  await tick(); await tick();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['t1', 't2'],
    'non-empty data must render via keyed diff');

  // Load empty data
  const second = startLoad();
  assert.ok(second);
  second.resolve({ tasks: [] });
  await tick(); await tick();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['empty'],
    'the empty frame must render via keyed diff');

  // Load data again — releases the placeholder
  const third = startLoad();
  assert.ok(third);
  third.resolve({ tasks: [{ task_id: 't3', state: 'done' }] });
  await tick(); await tick();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['t3'],
    'the empty placeholder must be released once data arrives');
});

/**
 * R4: 断点恢复的提示必须区分"重提了几个"与"在队未重复提交"。
 *
 * resume-pending 现在按 ledger 行的原身份认领（queue.claim），在队的行返回
 * already_queued 而不是 resumed。旧提示只看 resumed，会把"什么都没提交"报成
 * "无待恢复任务"，用户以为断点已经丢了（或以为按钮坏了）。这条源码契约钉住
 * 分支顺序：already_queued 必须先于 note 兜底命中。
 */
test('R4: the resume toast never reports 无待恢复任务 while rows are queued', () => {
  const src = fs.readFileSync(
    path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'views', 'tasks.js'),
    'utf8');
  const fn = src.slice(src.indexOf('async function resumePending()'));
  const body = fn.slice(0, fn.indexOf('\n}\n'));

  assert.ok(body.includes('r.already_queued > 0'), 'resumePending must branch on already_queued');
  assert.ok(
    body.indexOf('r.already_queued > 0') < body.indexOf("'无待恢复任务'"),
    'the already-queued branch must be tested before the 无待恢复任务 fallback');
  assert.ok(body.includes('已重提'), 'resumed rows are still reported as re-submitted');
});
