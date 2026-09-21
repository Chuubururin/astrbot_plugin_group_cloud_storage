/**
 * Unit tests: group open-state gate - refusal vs transport failure.
 *
 * M11: groups/open-state answers HTTP 200 with the verdict in the body, so a
 * timeout or a network blip landed in the same catch as a real refusal and
 * returned ok=false. Callers cleared currentGroup/folder/albumGroup/
 * essenceGroup unconditionally, so one failed probe dropped the user back to
 * the aggregate view even though nobody refused anything.
 *
 * Run: node --test pages/storage-ng/testing/unit/group-open-state.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};
globalThis.document = {
  createElement: () => ({ dataset: {}, children: [], classList: { add() {}, remove() {} } }),
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {},
};

const { checkGroupOpenable } = await import('../../features/group-open-state.js');

function withSdk(apiGet) {
  globalThis.window = { AstrBotPluginPage: { apiGet, apiPost: async () => ({}) } };
}

test('M11: a transport failure is flagged, not reported as a refusal', async () => {
  withSdk(async () => { throw new Error('apiGet timeout: groups/open-state'); });
  const gate = await checkGroupOpenable('123');
  assert.equal(gate.ok, false, 'an unknown verdict must not open the group');
  assert.equal(gate.transportError, true, 'a timeout must be flagged as a transport failure');
  assert.equal(gate.reason, null, 'a transport failure has no machine reason');
  assert.match(gate.label, /重试/, 'the label must offer a retry');
  delete globalThis.window;
});

test('M11: a real refusal carries the reason and no transport flag', async () => {
  withSdk(async () => ({ reason: 'group not managed' }));
  const gate = await checkGroupOpenable('123');
  assert.equal(gate.ok, false);
  assert.equal(gate.transportError, false, 'a body reason is a verdict, not a transport failure');
  assert.equal(gate.reason, 'group not managed');
  assert.equal(gate.label, '该群未受管理，暂不可操作');
  delete globalThis.window;
});

test('M11: a clean gate opens the group', async () => {
  withSdk(async () => ({ managed: true, account_online: true, seen: true, reason: null }));
  const gate = await checkGroupOpenable('123');
  assert.equal(gate.ok, true);
  assert.equal(gate.transportError, false);
  delete globalThis.window;
});

test('M11: group-data only clears the group focus on a real refusal', () => {
  const src = fs.readFileSync(path.join(here, '../../features/group-data.js'), 'utf8');
  const idx = src.indexOf('const gate = await checkGroupOpenable(g.group_id);');
  assert.ok(idx > -1, 'the group row click must run the open-state gate');
  const branch = src.slice(idx, idx + 400);
  assert.ok(branch.includes('if (!gate.transportError) clearGroupFocus();'),
    'clearGroupFocus must be guarded by the transportError flag');
  assert.ok(!/if \(!gate\.ok\) \{\s*\n\s*clearGroupFocus\(\);/.test(branch),
    'an unconditional clearGroupFocus on !gate.ok is the bug');
});
