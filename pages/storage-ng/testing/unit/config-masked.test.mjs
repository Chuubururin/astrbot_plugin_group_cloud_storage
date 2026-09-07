/**
 * Unit tests: config.js masked field behavior - node --test
 *
 * Verifies that the config view correctly handles masked fields:
 * - Masked fields with empty value should NOT be sent to backend
 * - Masked fields with "***" should NOT be sent to backend
 * - Non-masked fields should be sent normally
 *
 * Run: node --test pages/storage-ng/testing/unit/config-masked.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

// Simulate the valueOf logic from config.js
function valueOf(item, input) {
  if (item.type === 'bool') return input.checked;
  if (item.type === 'int') {
    const n = parseInt(String(input.value), 10);
    return Number.isNaN(n) ? null : n;
  }
  if (item.type === 'float') {
    const n = parseFloat(String(input.value));
    return Number.isNaN(n) ? null : n;
  }
  if (item.type === 'list') {
    try { return JSON.parse(input.value || '[]'); } catch (e) { return null; }
  }
  if (item.type === 'dict') {
    try { return JSON.parse(input.value || '{}'); } catch (e) { return null; }
  }
  return String(input.value);
}

// Simulate the saveConfig logic from config.js (post-fix)
function collectDirtyValues(items) {
  const values = {};
  for (const el of items) {
    if (el.dirty && el.getValue) {
      const v = el.getValue();
      if (v !== null) {
        // Masked fields: empty string = no change (留空则不修改)
        if (el.masked && (v === '' || v === '***')) continue;
        values[el.key] = v;
      }
    }
  }
  return values;
}

test('valueOf: string input returns string', () => {
  const item = { type: 'string' };
  const input = { value: 'hello' };
  assert.equal(valueOf(item, input), 'hello');
});

test('valueOf: empty string returns empty string', () => {
  const item = { type: 'string' };
  const input = { value: '' };
  assert.equal(valueOf(item, input), '');
});

test('valueOf: int type with valid number', () => {
  const item = { type: 'int' };
  const input = { value: '42' };
  assert.equal(valueOf(item, input), 42);
});

test('valueOf: int type with invalid number returns null', () => {
  const item = { type: 'int' };
  const input = { value: 'abc' };
  assert.equal(valueOf(item, input), null);
});

test('valueOf: bool type with checkbox', () => {
  const item = { type: 'bool' };
  const input = { checked: true };
  assert.equal(valueOf(item, input), true);
});

test('masked field with empty value is excluded', () => {
  const items = [
    { key: 'openlist_password', dirty: true, masked: true, getValue: () => '' },
    { key: 'request_interval_ms', dirty: true, masked: false, getValue: () => 500 },
  ];
  const values = collectDirtyValues(items);
  assert.equal(values.openlist_password, undefined);
  assert.equal(values.request_interval_ms, 500);
});

test('masked field with "***" is excluded', () => {
  const items = [
    { key: 'openlist_token', dirty: true, masked: true, getValue: () => '***' },
    { key: 'some_key', dirty: true, masked: false, getValue: () => 'val' },
  ];
  const values = collectDirtyValues(items);
  assert.equal(values.openlist_token, undefined);
  assert.equal(values.some_key, 'val');
});

test('masked field with actual value is included', () => {
  const items = [
    { key: 'openlist_password', dirty: true, masked: true, getValue: () => 'new_pass' },
  ];
  const values = collectDirtyValues(items);
  assert.equal(values.openlist_password, 'new_pass');
});

test('non-masked field with empty value is included', () => {
  const items = [
    { key: 'some_setting', dirty: true, masked: false, getValue: () => '' },
  ];
  const values = collectDirtyValues(items);
  // Non-masked empty strings should be included (backend handles validation)
  assert.equal(values.some_setting, '');
});

test('clean (non-dirty) items are excluded', () => {
  const items = [
    { key: 'a', dirty: false, masked: false, getValue: () => 'val' },
    { key: 'b', dirty: true, masked: false, getValue: () => 'val2' },
  ];
  const values = collectDirtyValues(items);
  assert.equal(values.a, undefined);
  assert.equal(values.b, 'val2');
});

test('null getValue result is excluded', () => {
  const items = [
    { key: 'a', dirty: true, masked: false, getValue: () => null },
  ];
  const values = collectDirtyValues(items);
  assert.equal(values.a, undefined);
});

test('multiple masked fields all excluded when empty', () => {
  const items = [
    { key: 'openlist_password', dirty: true, masked: true, getValue: () => '' },
    { key: 'openlist_token', dirty: true, masked: true, getValue: () => '' },
    { key: 'download_token', dirty: true, masked: true, getValue: () => '' },
    { key: 'normal_key', dirty: true, masked: false, getValue: () => 'keep' },
  ];
  const values = collectDirtyValues(items);
  assert.deepEqual(Object.keys(values), ['normal_key']);
  assert.equal(values.normal_key, 'keep');
});
