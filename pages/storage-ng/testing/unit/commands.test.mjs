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
  await commands.runCommand('test-ping', { keys: [1], rows: [] }, {
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