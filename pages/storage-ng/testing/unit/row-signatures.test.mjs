/**
 * Unit tests: render-signature projections (features/row-signatures).
 *
 * structure.test.mjs already enforces the coverage half of the signature
 * contract: every field a row builder reads must appear in its signature. This
 * file covers the half a source scan cannot see - that a projected field can
 * actually *move* the signature. A field whose projection collapses (an object
 * joined through Array.prototype.join renders as "[object Object]", the same
 * string for every object) keeps its row pinned to stale DOM forever, and every
 * coverage test still passes.
 *
 * Run: node --test pages/storage-ng/testing/unit/row-signatures.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { rowSignature, groupSignature, joinSignature } from '../../features/row-signatures.js';

/** A resource row as webapi/resources.py serializes it (tags is a list there). */
const fileItem = (over = {}) => ({
  is_up: 0, is_dir: 0, is_folder: 0,
  name: 'a.mp4', type: 'video', size: 10, uploader: 'u', modified: 1, created: 2,
  is_volume: 0, volume_total: 0, volume_complete: 0, volume_done: 0,
  is_long: 0, indexed_at: 3, tags: ['x'], ...over,
});

test('rowSignature: a list-valued projected field is sensitive to element content', () => {
  // `tags` is `list(it.tags)` on the wire, and the row renders it as one badge,
  // so the projection must distinguish one tag set from another.
  assert.notEqual(rowSignature(fileItem({ tags: ['a', 'b'] })),
    rowSignature(fileItem({ tags: ['a', 'c'] })));
  assert.notEqual(rowSignature(fileItem({ tags: [] })), rowSignature(fileItem({ tags: ['a'] })));
});

test('rowSignature: an object-valued projected field is sensitive to its content', () => {
  // The regression this guards is silent in the DOM: add a field to the
  // signature that can hold a plain object and, without serialization, every
  // value of it collapses to "[object Object]".
  assert.notEqual(rowSignature(fileItem({ indexed_at: { v: 1 } })),
    rowSignature(fileItem({ indexed_at: { v: 2 } })));
  assert.notEqual(groupSignature({ shown_name: 'g', last_scan: { at: 1 } }),
    groupSignature({ shown_name: 'g', last_scan: { at: 2 } }));
});

test('rowSignature: every projected field can move the signature on its own', () => {
  const fields = ['is_up', 'is_dir', 'is_folder', 'name', 'type', 'size', 'uploader',
    'modified', 'created', 'is_volume', 'volume_total', 'volume_complete',
    'volume_done', 'is_long', 'indexed_at', 'tags'];
  // Falsy vs truthy: the boolean-flag slots project through `? 1 : 0`, so two
  // distinct strings would collapse there for the right reason.
  for (const f of fields) {
    assert.notEqual(rowSignature(fileItem({ [f]: 0 })),
      rowSignature(fileItem({ [f]: 'probe-truthy' })),
      `${f} is projected but cannot change the signature`);
  }
});

test('joinSignature: absent, null and empty join alike, as Array.join did', () => {
  // The projection switched from a raw join to a serialized one; the
  // null/undefined rendering must not move, or a field that the API omits for
  // some rows would look changed on every poll.
  assert.equal(joinSignature([null]), joinSignature([undefined]));
  assert.equal(joinSignature([undefined]), joinSignature([]));
  assert.equal(rowSignature(fileItem({ tags: null })), rowSignature(fileItem({ tags: undefined })));
});
