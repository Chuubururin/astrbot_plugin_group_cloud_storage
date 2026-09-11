/**
 * Unit tests: command registry + lifecycle (features/commands.js +
 * command-defs.js + command-defs-netdisk.js).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// Minimal DOM stub so toast/modal imports resolve in node.
const makeEl = () => ({
  className: '', classList: { add() {}, remove() {}, toggle() {} },
  dataset: {}, children: [], style: {}, innerHTML: '', textContent: '',
  appendChild() {}, remove() {}, addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, setAttribute() {},
});
globalThis.document = {
  body: makeEl(),
  createElement: () => makeEl(),
  addEventListener() {},
  querySelector() { return null; },
};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

await import(join(root, 'features', 'command-registry.js')).then((m) => m.registerAllCommands());
const commands = await import(join(root, 'features', 'commands.js'));

test('registry: command definitions registered', () => {
  const all = commands.commands();
  assert.ok(all.download, 'download registered');
  assert.ok(all.delete, 'delete registered');
  // 2026-09-06 整改：bridge-out 移除——转存网盘是下载的特殊形式
  // （files-distribute → downloadItems('netdisk')，按群批量 bridge/transfer）
  assert.ok(!all['bridge-out'], 'bridge-out removed (download target=netdisk)');
  assert.ok(all.rename, 'rename registered');
  // 2026-09-03 整改（S2）：transfer-in 已移除（与分发 target=group 重复）；分发命令保留
  assert.ok(!all['transfer-in'], 'transfer-in removed (duplicate of distribute target=group)');
  assert.ok(all['netdisk-distribute'], 'netdisk-distribute registered (netdisk domain)');
  assert.ok(all['files-distribute'], 'files-distribute registered (W2-A)');
  assert.ok(all['essence-distribute'], 'essence-distribute registered (W2-A)');
  assert.equal(all.delete.danger, true);
  assert.equal(all.rename.needsSingle, true);
});

test('canRun: preconditions', () => {
  assert.equal(commands.canRun('delete', { count: 0 }).ok, false);
  assert.equal(commands.canRun('delete', { count: 2 }).ok, true);
  assert.equal(commands.canRun('rename', { count: 2 }).ok, false, 'single required');
  assert.equal(commands.canRun('move', { count: 1, hasGroup: false }).ok, false);
  assert.equal(commands.canRun('move', { count: 1, hasGroup: true }).ok, true);
  assert.equal(commands.canRun('unknown-xyz', { count: 1 }).ok, false);
});

test('resolveButtons: capability -> button mapping with disabled flags', () => {
  const btns = commands.resolveButtons(['download', 'rename', 'delete', 'not-a-command'], { count: 2, hasGroup: true });
  assert.equal(btns.length, 3, 'unknown capability filtered');
  const rename = btns.find((b) => b.id === 'rename');
  assert.equal(rename.disabled, true, 'rename needs single, count=2 -> disabled');
  assert.ok(rename.reason);
  const del = btns.find((b) => b.id === 'delete');
  assert.equal(del.disabled, false);
});

test('runCommand: lifecycle smoke (busy on/off + run + done)', async () => {
  const ran = [];
  commands.registerCommand({
    id: 'test-ping',
    label: 'ping',
    run(ctx) { ran.push(ctx.keys); return Promise.resolve(); },
  });
  const busyCalls = [];
  let doneCall = 0;
  await commands.runCommand('test-ping', { keys: [1], rows: [{ id: 1 }] }, {
    onBusy: (id) => { busyCalls.push(id); },
    onDone: () => { doneCall++; },
  });
  assert.deepEqual(ran, [[1]], 'run received keys');
  assert.deepEqual(busyCalls, ['test-ping'], 'busy on sequence');
  assert.equal(doneCall, 1);
  commands.unregisterCommand('test-ping');
  assert.equal(commands.commands()['test-ping'], undefined);
});

test('runCommand: precondition gate blocks execution', async () => {
  let ran = false;
  commands.registerCommand({
    id: 'test-gated',
    label: 'gated',
    run() { ran = true; return Promise.resolve(); },
  });
  await commands.runCommand('test-gated', { keys: [], rows: [] }, {});
  assert.equal(ran, false, 'no selection -> blocked before run');
  commands.unregisterCommand('test-gated');
});

test('canRunRowAware: row-derived group enables group commands in aggregate view', () => {
  assert.equal(commands.canRun('move', { count: 1, hasGroup: false }).ok, false);
  assert.equal(
    commands.canRunRowAware('move', { count: 1, hasGroup: false, rowsHaveGroup: true }).ok,
    true,
    'row-aware enables group command');
  assert.equal(
    commands.canRunRowAware('move', { count: 1, hasGroup: false, rowsHaveGroup: false }).ok,
    false);
});

test('runCommand: needsGroup precondition (row-aware path)', async () => {
  let ran = false;
  commands.registerCommand({
    id: 'test-ng',
    label: 'ng',
    needsGroup: true,
    run() { ran = true; return Promise.resolve(); },
  });
  await commands.runCommand('test-ng', { keys: [1], rows: [], rowAware: false }, {});
  assert.equal(ran, false, 'no group context -> blocked');
  await commands.runCommand('test-ng', { keys: [1], rows: [{ id: 1, group_id: 'g1' }], rowAware: true }, {});
  assert.equal(ran, true, 'row-aware group context -> runs');
  commands.unregisterCommand('test-ng');
});

test('runCommand: user-facing gate reasons are localized', () => {
  // 行业惯例: 用户可见文案与代码内部消息隔离 (MS .NET localization model);
  // canRun 的 reason 直接进 toast, 必须是中文。
  for (const [id, env] of [
    ['delete', { count: 0 }],
    ['rename', { count: 2 }],
    ['move', { count: 1, hasGroup: false }],
  ]) {
    const check = commands.canRun(id, env);
    assert.equal(check.ok, false);
    assert.ok(!/[a-z]{4,}/i.test(check.reason.replace(/[zh]/g, '')) || /[\u4e00-\u9fff]/.test(check.reason),
      `reason should be localized: ${check.reason}`);
  }
});

test('runCommand: cancel keeps the selection and skips refresh (no side effects)', async () => {
  // 取消对话框不应产生副作用 (各 HIG 通用要求): 选择保持、不刷新。
  const cleared = { sel: false };
  commands.registerCommand({
    id: 'test-cancel',
    label: 'cancelable',
    refresh: ['files'],
    run() { return null; }, // 模态取消路径: run 正常返回 null
  });
  const fakeSel = { clear: () => { cleared.sel = true; } };
  await commands.runCommand('test-cancel', {
    keys: ['1'], rows: [{ id: 1 }], source: { selection: fakeSel }, rowAware: true,
  }, {});
  assert.equal(cleared.sel, false, 'cancel must not clear the selection');

  // 非 null 返回值: 照常清选择
  commands.registerCommand({
    id: 'test-ok',
    label: 'ok',
    run() { return Promise.resolve('done'); },
  });
  await commands.runCommand('test-ok', {
    keys: ['1'], rows: [{ id: 1 }], source: { selection: fakeSel }, rowAware: true,
  }, {});
  assert.equal(cleared.sel, true, 'successful run clears the selection');
  commands.unregisterCommand('test-cancel');
  commands.unregisterCommand('test-ok');
});