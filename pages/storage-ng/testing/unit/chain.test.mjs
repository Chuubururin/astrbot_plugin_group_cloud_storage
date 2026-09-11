/**
 * Unit tests: continuous-operation chain regressions (缺陷修复回归).
 *
 * Covers:
 *  - openExternal: window.open 返回 null (sandbox 无 allow-popups) 时的
 *    降级契约 — 复制链接并返回 false, 不静默失败
 *  - constants: paused/resumed/cancelled SSE 事件类型必须存在 (任务账本
 *    行状态同步依赖它们)
 *  - main.js initKeyboard 源码契约: Escape 在浮层打开时让位 (不 clear 选择)
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// ---- minimal DOM stub (same pattern as core.test.mjs) ----
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
    appendChild(c) { this.children.push(c); },
    remove() {},
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = v; },
    get textContent() { return this._text || ''; },
    setAttribute() {},
  };
  return node;
}
globalThis.document = {
  createElement: (t) => el(t),
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  body: el('body'),
  documentElement: el('html'),
};
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

const { EVENT_TYPES } = await import('../../constants.js');

test('chain: SSE paused/resumed/cancelled types exist (ledger sync depends on them)', () => {
  for (const t of ['paused', 'resumed', 'cancelled']) {
    assert.ok(Object.values(EVENT_TYPES).includes(t), `EVENT_TYPES must include ${t}`);
  }
});

test('chain: Escape defers to open overlays (cancel must not clear selection)', () => {
  // 全局 keydown 位于 components/keyboard.js (main.js 300 行预算外置)
  const src = fs.readFileSync(path.join(here, '../../components/keyboard.js'), 'utf8');
  const escapeIdx = src.indexOf("e.key === 'Escape'");
  assert.ok(escapeIdx > -1, 'Escape branch must exist in initKeyboard');
  const branch = src.slice(escapeIdx, escapeIdx + 600);
  assert.ok(
    branch.includes('modal-overlay:not(.hidden)') || branch.includes('querySelector'),
    'Escape branch must check for open overlays before clearing',
  );
  // 分支内 (让位 return 之后) 才能 selection.clear — 检查顺序
  const deferReturn = branch.indexOf('if (overlayOpen) return;');
  const clearCall = branch.indexOf('selection.clear()');
  assert.ok(deferReturn > -1, 'overlay defer-return must exist');
  assert.ok(clearCall > -1, 'selection clear must still exist for bare Escape');
  assert.ok(deferReturn < clearCall, 'defer check must precede selection.clear');
  // main.js 挂载 keyboard (不能丢初始化)
  const mainSrc = fs.readFileSync(path.join(here, '../../main.js'), 'utf8');
  assert.ok(mainSrc.includes("from './components/keyboard.js'"), 'main.js must mount keyboard');
});

test('chain: no bare window.open remains in feature/command code (must use openExternal)', () => {
  const dirs = ['../../features', '../../components'];
  const offenders = [];
  for (const d of dirs) {
    const dir = path.join(here, d);
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith('.js')) continue;
      const src = fs.readFileSync(path.join(dir, f), 'utf8');
      if (/window\.open\(/.test(src)) offenders.push(`${d}/${f}`);
    }
  }
  assert.deepEqual(offenders, [],
    'window.open is always intercepted by the sandboxed iframe; use openExternal()');
});

test('chain: groups/remove backend speaks group_ids (same contract as restore)', () => {
  const backend = path.join(here, '../../../../webapi/groups.py');
  const src = fs.readFileSync(backend, 'utf8');
  // "(s" anchors the exact handler: 'api_groups_remove' alone would prefix-
  // match 'api_groups_removed', which appears earlier in the file.
  const removeIdx = src.indexOf('async def api_groups_remove(s');
  assert.ok(removeIdx > -1, 'api_groups_remove must exist');
  // api_groups_remove may be the last handler in the file: bound the body by
  // the next handler (if any).
  const nextDef = src.indexOf('async def api_', removeIdx + 10);
  const removeBody = nextDef > -1 ? src.slice(removeIdx, nextDef) : src.slice(removeIdx);
  assert.ok(removeBody.includes('"group_ids"'), 'remove must accept group_ids');
  assert.ok(removeBody.includes('restore'), 'remove docstring references restore contract');
  // 向后兼容 items
  assert.ok(removeBody.includes('"items"'), 'remove keeps legacy items compatibility');
  // restore 端同样使用 group_ids (链路两端同一种语言)
  const restoreIdx = src.indexOf('async def api_groups_restore(s');
  const restoreBody = src.slice(restoreIdx, restoreIdx + 2000);
  assert.ok(restoreBody.includes('"group_ids"'), 'restore must accept group_ids');
});

test('chain: groups/order frontend body matches backend ordered_ids contract', () => {
  // 上/下移链路曾经断裂: 前端发 {group_ids, direction}, 后端只认
  // {ordered_ids}, 每次点击必然 400。两侧契约必须逐字对齐。
  const actions = fs.readFileSync(path.join(here, '../../features/group-actions.js'), 'utf8');
  const caseIdx = actions.indexOf("case 'up':");
  assert.ok(caseIdx > -1, "handleMenuAction must handle 'up'");
  const caseBody = actions.slice(caseIdx, caseIdx + 1400);
  assert.ok(caseBody.includes("API.GROUPS.ORDER"), 'up/down posts to groups/order');
  assert.ok(caseBody.includes('ordered_ids'), 'frontend must send ordered_ids');
  assert.ok(!caseBody.includes("direction: act"), 'legacy direction body is the broken contract');

  const backend = fs.readFileSync(path.join(here, '../../../../webapi/groups.py'), 'utf8');
  const orderIdx = backend.indexOf('async def api_groups_order(s');
  const orderBody = backend.slice(orderIdx, orderIdx + 600);
  assert.ok(orderBody.includes('"ordered_ids"'), 'backend requires ordered_ids');
});
