/**
 * Unit tests: upload-name limits (prepare contract name 1..80).
 *
 * M10: sanitizeUploadName() reserved NAME_MAX_LEN - ext.length for the stem,
 * but when the extension itself is longer than the limit the stem was clamped
 * to 1 char and the reassembled name still exceeded 80 -> prepare 400. Trigger:
 * a batch naming template passing a name through verbatim, e.g. `a.` + 100
 * chars of "extension".
 *
 * L8: uniqueNames() appended the (2) dedupe suffix AFTER the length cap, so a
 * template-rendered 80-char name became 83 chars once deduped -> 400 again.
 *
 * L11: the files-tab album source still advertised 图片/视频 even though every
 * album entry refuses video locally (features/upload.js, features/ingest.js).
 *
 * Run: node --test pages/storage-ng/testing/unit/ingest-name-limits.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  NAME_MAX_LEN, UPLOAD_SOURCE_OPTIONS, sanitizeUploadName, uniqueNames,
} from '../../features/ingest-options.js';

const fits = (s) => s.length >= 1 && s.length <= NAME_MAX_LEN;

test('M10: an over-long extension still yields a name inside 1..80', () => {
  const raw = `a.${'x'.repeat(100)}`;
  assert.ok(raw.length > NAME_MAX_LEN, 'fixture must exceed the contract length');
  const out = sanitizeUploadName(raw, '');
  assert.ok(fits(out), `expected 1..${NAME_MAX_LEN} chars, got ${out.length}`);
});

test('M10: every over-long shape lands inside 1..80', () => {
  const cases = [
    `a.${'x'.repeat(200)}`,
    'z'.repeat(300),
    `.${'q'.repeat(120)}`,
    `${'y'.repeat(90)}.${'p'.repeat(90)}`,
    `a.${'x'.repeat(78)}`,
  ];
  for (const raw of cases) {
    const out = sanitizeUploadName(raw, '');
    assert.ok(fits(out), `${raw.length} chars in -> ${out.length} chars out`);
  }
});

test('M10: the ordinary over-long name still keeps its extension', () => {
  const out = sanitizeUploadName(`${'n'.repeat(100)}.txt`, '');
  assert.ok(fits(out), `got ${out.length} chars`);
  assert.ok(out.endsWith('.txt'), `the extension must survive: ${out}`);
});

test('L8: dedupe suffixes keep names inside the contract length', () => {
  const base = `${'n'.repeat(NAME_MAX_LEN - 4)}.txt`;
  assert.equal(base.length, NAME_MAX_LEN, 'fixture must sit exactly at the limit');
  const out = uniqueNames([base, base, base]);
  assert.equal(out.length, 3);
  assert.equal(new Set(out.map((s) => s.toLowerCase())).size, 3, 'names must stay distinct');
  for (const n of out) {
    assert.ok(fits(n), `${n.length} chars exceeds ${NAME_MAX_LEN}: ${n}`);
  }
  assert.equal(out[0], base, 'the first occurrence stays untouched');
  assert.ok(out[1].includes('(2)'), `the dedupe suffix must survive: ${out[1]}`);
  assert.ok(out[1].endsWith('.txt'), `the extension must survive: ${out[1]}`);
});

test('L8: short duplicate names keep the plain (2) suffix', () => {
  assert.deepEqual(uniqueNames(['a.txt', 'a.txt', 'b.txt']),
    ['a.txt', 'a(2).txt', 'b.txt']);
});

test('L11: the files-tab album source advertises images only', () => {
  const album = UPLOAD_SOURCE_OPTIONS.find((o) => o.value === 'album');
  assert.ok(album, 'the album source stays listed on the files tab');
  assert.ok(!/视频|video/i.test(album.label),
    `the album source must not promise video: ${album.label}`);
  assert.match(album.label, /仅图片/);
});
