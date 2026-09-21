/**
 * Unit tests: tasks view loading - request-sequence guard + keyed empty state.
 *
 * M9: loadTasks() is fired from six entry points (three filter subscriptions,
 * paging, the refresh button, post-action reloads) but wrote taskLedger and the
 * pager meta unguarded after `await`, so a slow older response could land last
 * and desync "第 N 页" / the next-page button from the rows on screen.
 * data-table / bridge-panel / stat-bar all carry the same nextSeq/isStale guard.
 *
 * L12: the empty state wrote tbody.innerHTML directly, which never bumps the
 * keyed-diff run generation, so a non-empty render already queued in rAF still
 * appended its rows behind the "暂无任务" row.
 *
 * Run: node --test pages/storage-ng/testing/unit/tasks-load.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

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

const tick = () => new Promise((r) => setTimeout(r, 5));

function startLoad() {
  const n = pending.length;
  refresh('tasks');
  assert.equal(pending.length, n + 1, 'refresh:tasks must fire one ledger load');
  return pending[n];
}

test('M9: a slow older ledger response cannot overwrite a newer one', async () => {
  const first = pending[0];
  const second = startLoad();
  second.resolve({ tasks: [{ task_id: 'new', state: 'pending' }] });
  await tick();
  first.resolve({ tasks: [{ task_id: 'old', state: 'pending' }] });
  await tick();
  assert.deepEqual(getState().taskLedger.map((t) => t.task_id), ['new'],
    'the superseded response must be dropped (last-request-wins)');
});

test('L12: the empty state goes through the keyed diff', async () => {
  const tbody = byId.get('task-tbody');
  tbody.children.length = 0;
  const first = startLoad();
  const second = startLoad();
  first.resolve({ tasks: [{ task_id: 't1', state: 'pending' }, { task_id: 't2', state: 'pending' }] });
  second.resolve({ tasks: [] });
  await tick();
  await tick();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['empty'],
    'the superseded non-empty frame must not append its rows behind the empty row');

  // The placeholder is a keyed row: real data releases it.
  const third = startLoad();
  third.resolve({ tasks: [{ task_id: 't3', state: 'done' }] });
  await tick();
  await tick();
  assert.deepEqual(tbody.children.map((c) => c.dataset.key), ['t3'],
    'the empty placeholder must be released once data arrives');
});
