/**
 * Unit tests: N-07/N-08 behavior (defaults, ../ up-level, dictionaries)
 * and data sources param mapping.
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

// ---- minimal DOM stub (same pattern as core.test.mjs) ----
const elements = new Map();
function el(tag) {
  const node = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    isConnected: false,
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); },
      contains(c) { return this._set.has(c); },
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    appendChild(c) {
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
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = v; },
    get textContent() { return this._text || ''; },
    setAttribute() {},
    set id(v) { this._id = v; elements.set(v, this); },
    get id() { return this._id; },
  };
  return node;
}

globalThis.document = {
  createElement: (t) => el(t),
  createDocumentFragment: () => el('fragment'),
  getElementById: (id) => elements.get(id) || null,
  querySelectorAll: () => [],
  addEventListener() {},
  documentElement: el('html'),
  readyState: 'complete',
};

function flushRAF() {
  return new Promise((r) => setTimeout(() => globalThis.requestAnimationFrame(() => r()), 0));
}
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

const store = await import('../../store.js');

test('N-07: default sort is created_at desc (newest first)', () => {
  assert.deepEqual(store.getState().fileSort, { by: 'created_at', dir: 'desc' });
});

test('N-07: default layout is single-column; dual only after explicit choice', () => {
  assert.equal(store.getState().layout, 'single');
  store.setLayout('dual');
  assert.equal(store.getState().layout, 'dual');
  assert.equal(localStorage.getItem('cs_layout'), 'dual');
  store.setLayout('single');
  assert.equal(store.getState().layout, 'single');
});

test('N-02: fileStatus initial empty and settable', () => {
  assert.equal(store.getState().fileStatus, '');
  store.set('fileStatus', 'netdisk');
  assert.equal(store.getState().fileStatus, 'netdisk');
  store.set('fileStatus', '');
});

// ---- file-rows ../ up-level (N-08) ----
const rowsMod = await import('../../features/file-rows.js');

function makeContainer() {
  return {
    querySelector(sel) {
      if (sel === '.file-tbody[data-pane="a"]') return paneA;
      if (sel === '.file-tbody[data-pane="b"]') return paneB;
      return null;
    },
  };
}
let paneA, paneB;
function freshPanes() {
  paneA = el('tbody');
  paneB = el('tbody');
  paneA.dataset.pane = 'a';
  paneB.dataset.pane = 'b';
  paneA.querySelectorAll = () => [];
  paneA.querySelector = () => null;
  paneB.querySelectorAll = () => [];
  paneB.querySelector = () => null;
}

test('N-08: renderRows adds ../ row when inside a folder (group source)', async () => {
  freshPanes();
  store.set('folder', '文档');
  store.set('folderChain', [{ name: '文档' }]);
  store.set('layout', 'single');
  const src = { id: 'group', rowKey: (f) => String(f.id) };
  rowsMod.renderRows(makeContainer(), src, [{ id: 1, name: 'a.pdf' }], [{ id: 9, name: '文档' }]);
  await flushRAF();
  const rows = paneA.children[0]?.children || [];
  assert.deepEqual(rows.map((c) => c.dataset.key), ['dir:..', 'dir:9', '1']);
});

test('N-08: no ../ row at root (folder empty)', async () => {
  freshPanes();
  store.set('folder', '');
  store.set('folderChain', []);
  store.set('layout', 'single');
  const src = { id: 'group', rowKey: (f) => String(f.id) };
  rowsMod.renderRows(makeContainer(), src, [{ id: 1, name: 'a.pdf' }], []);
  await flushRAF();
  const rows = paneA.children[0]?.children || [];
  assert.deepEqual(rows.map((c) => c.dataset.key), ['1']);
});

test('N-08: rowKeyOf prefixes up row as dir:.. without collision', () => {
  assert.equal(rowsMod.rowKeyOf({ id: 'group' }, { is_up: true, id: '..' }), 'dir:..');
});

// ---- data sources ----
const ds = await import('../../features/data-sources.js');

test('N-01: GROUP_SOURCE keeps the full capability set (功能只增不减)', () => {
  // 2026-09-06 下载整合：bridge-out/volumes/verify 退出能力表（转存并入
  // files-distribute 目标），download/link/address 也并入 files-distribute，
  // 集合收敛为 7 项。
  assert.ok(ds.GROUP_SOURCE.capabilities.length >= 7, 'full capability set retained');
});

test('N-01: applyLocalFilterSort folder class matches no file rows', () => {
  const items = [{ name: 'a.mp4', type: 'video' }];
  const out = ds.applyLocalFilterSort(items, { type: 'folder', sort_by: '', sort_dir: 'asc' }, null);
  assert.deepEqual(out, []);
});

// ---- api.js dictionaries ----
test('N-01: TYPE_LABELS covers the 13 classification machine values', async () => {
  const { TYPE_LABELS, STORE_STATUS_LABELS } = await import('../../api.js');
  for (const t of ['document', 'pdf', 'spreadsheet', 'slide', 'online_doc',
    'image', 'video', 'audio', 'archive', 'installer', 'flash', 'folder', 'other']) {
    assert.ok(TYPE_LABELS[t], `label for ${t}`);
  }
  for (const s of ['netdisk', 'album', 'essence', 'none']) {
    assert.ok(STORE_STATUS_LABELS[s], `status label ${s}`);
  }
});

// ---- N-10 set semantics: volume/long badges ----
test('N-10: buildRow renders long-set badge for logical sets', () => {
  const src = { id: 'group', rowKey: (f) => String(f.id) };
  const tr = rowsMod.buildRow(src, {
    id: 42, name: '长视频合集.mp4', size: 999, is_volume: false, is_long: true, type: 'video',
  });
  assert.ok((tr.innerHTML || '').includes('长集'), 'long-set badge');
});

test('N-10: no badges for plain file', () => {
  const src = { id: 'group', rowKey: (f) => String(f.id) };
  const tr = rowsMod.buildRow(src, { id: 7, name: '普通.txt', size: 1, type: 'document' });
  assert.ok(!(tr.innerHTML || '').includes('分卷'), 'no volume badge');
  assert.ok(!(tr.innerHTML || '').includes('长集'), 'no long badge');
});