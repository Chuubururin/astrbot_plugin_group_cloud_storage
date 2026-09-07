/**
 * Unit tests: helpers + constants maps - node --test (TE-3 layer 2)
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { formatSize, truncate, cls } from '../../utils/helpers.js';
import { DATA_CHANGED_TOPICS, MAX_ROWS_PER_FRAME, EVENT_TYPES } from '../../constants.js';
import { rectHitsRow } from '../../features/marquee-select.js';

test('formatSize: units and zero', () => {
  assert.equal(formatSize(0), '0 B');
  assert.equal(formatSize(1023), '1023 B');
  assert.equal(formatSize(1024), '1.0 KB');
  assert.equal(formatSize(95 * 1024 * 1024), '95.0 MB');
  assert.equal(formatSize(undefined), '-');
});

test('truncate: keeps short strings, ellipsizes long ones', () => {
  assert.equal(truncate('abc', 10), 'abc');
  assert.equal(truncate('abcdefgh', 5).length, 5);
  assert.equal(truncate(undefined, 5), '');
});

test('cls: joins truthy class names', () => {
  assert.equal(cls('a', '', 'b'), 'a b');
});

test('DATA_CHANGED_TOPICS: every known kind maps to valid topics', () => {
  const known = new Set(['files', 'groups', 'bridge', 'netdisk', 'albums', 'essence']);
  for (const topics of Object.values(DATA_CHANGED_TOPICS)) {
    assert.ok(topics.length > 0);
    for (const t of topics) {
      assert.ok(known.has(t), `topic ${t}`);
    }
  }
  // scan family hits groups; mutation family hits files
  assert.ok(DATA_CHANGED_TOPICS.scan.includes('groups'));
  assert.ok(DATA_CHANGED_TOPICS.upload.includes('files'));
  assert.ok(DATA_CHANGED_TOPICS.delete.includes('files'));
  assert.ok(DATA_CHANGED_TOPICS.essence_save.includes('essence'));
  assert.ok(DATA_CHANGED_TOPICS.video_album.includes('albums'));
});

test('MAX_ROWS_PER_FRAME: budget is 50 (FE-11/12)', () => {
  assert.equal(MAX_ROWS_PER_FRAME, 50);
});

test('EVENT_TYPES: heartbeat registered (CT-6 / I5)', () => {
  assert.equal(EVENT_TYPES.HEARTBEAT, 'heartbeat');
});

test('rectHitsRow: viewport intersection semantics (C6/I10)', () => {
  const row = { left: 10, right: 100, top: 10, bottom: 20 };
  assert.equal(rectHitsRow(row, { x0: 0, x1: 50, y0: 0, y1: 15 }), true);
  assert.equal(rectHitsRow(row, { x0: 0, x1: 5, y0: 0, y1: 100 }), false);
  assert.equal(rectHitsRow(row, { x0: 0, x1: 500, y0: 30, y1: 40 }), false);
});
