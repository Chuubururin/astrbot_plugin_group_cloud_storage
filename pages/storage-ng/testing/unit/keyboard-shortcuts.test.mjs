/**
 * Unit tests: global keyboard shortcuts (Escape / Ctrl+A).
 *
 * Regression guard for the broken dynamic import in components/keyboard.js:
 * `import('./data-sources.js')` resolved to components/data-sources.js, which
 * does not exist (sourceFor lives in features/data-sources.js). Every Escape /
 * Ctrl+A keydown therefore rejected with ERR_MODULE_NOT_FOUND inside an async
 * listener - an unhandled rejection that made both shortcuts do nothing and
 * left the NO_LIST_SELECTION_VIEWS guard unreachable.
 *
 * Run: node --test pages/storage-ng/testing/unit/keyboard-shortcuts.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// ---- minimal DOM stub (same pattern as chain.test.mjs) ----
function el(tag = 'div') {
  return {
    tagName: String(tag).toUpperCase(),
    dataset: {}, children: [], style: {}, className: '', textContent: '', innerHTML: '',
    isConnected: false, parentNode: null,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild() {}, remove() {}, setAttribute() {}, click() {},
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  };
}

const listeners = new Map();
globalThis.document = {
  body: el('body'),
  createElement: (t) => el(t),
  createDocumentFragment: () => el('fragment'),
  getElementById: () => null,
  addEventListener(type, fn) { listeners.set(type, fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  readyState: 'complete',
};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

const { initKeyboard } = await import('../../components/keyboard.js');
const { getState, set } = await import('../../store.js');

initKeyboard();
const keydown = listeners.get('keydown');
assert.ok(keydown, 'initKeyboard must register a keydown listener');

/** Dispatch one synthetic keydown and settle the async handler. */
const press = (ev) => keydown({ preventDefault() {}, target: { tagName: 'DIV' }, ...ev });

test('Escape clears the active list selection (the lazy sourceFor import resolves)', async () => {
  set('currentView', 'files');
  set('fileSelected', new Set(['7']));
  await press({ key: 'Escape' });
  assert.equal(getState().fileSelected.size, 0,
    'Escape must clear fileSelected; a rejected dynamic import silently skips it');
});

test('Ctrl+A selects every non-directory row of the active list', async () => {
  set('currentView', 'files');
  set('fileSelected', new Set());
  set('fileItems', [
    { id: 1, is_dir: false }, { id: 2, is_dir: true }, { id: 3, is_dir: false },
  ]);
  await press({ key: 'a', ctrlKey: true });
  assert.deepEqual([...getState().fileSelected].sort(), ['1', '3'],
    'Ctrl+A must select the visible files and skip folders');
});

test('views with no list selection stay untouched (guard now reachable)', async () => {
  set('currentView', 'tasks');
  set('fileSelected', new Set(['7']));
  await press({ key: 'Escape' });
  assert.deepEqual([...getState().fileSelected], ['7'],
    'Escape must not rewrite the files selection from the tasks tab');
  await press({ key: 'a', ctrlKey: true });
  assert.deepEqual([...getState().fileSelected], ['7'],
    'Ctrl+A must not rewrite the files selection from the tasks tab');
});

test('every dynamic import in keyboard.js resolves to an existing module', () => {
  const file = path.join(here, '../../components/keyboard.js');
  const src = fs.readFileSync(file, 'utf8');
  const specs = [...src.matchAll(/import\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]);
  assert.ok(specs.length >= 2, 'Escape and Ctrl+A both resolve sourceFor lazily');
  for (const spec of specs) {
    const target = path.resolve(path.dirname(file), spec);
    assert.ok(fs.existsSync(target),
      `dynamic import '${spec}' resolves to ${target}, which does not exist`);
  }
});
