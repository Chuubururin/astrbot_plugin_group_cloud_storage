/**
 * Unit tests: tasks.js API references - node --test
 *
 * Verifies that all API.* references in tasks.js are valid and resolve to
 * non-undefined string paths (regression for the TASKS.PAUSE → TASKS_PAUSE bug).
 *
 * Run: node --test pages/storage-ng/testing/unit/tasks-api.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { API } from '../../api.js';

test('TASKS is a string path (not an object)', () => {
  assert.equal(typeof API.TASKS, 'string');
  assert.equal(API.TASKS, 'tasks');
});

test('TASKS_QUEUE is defined and is a string', () => {
  assert.equal(typeof API.TASKS_QUEUE, 'string');
  assert.equal(API.TASKS_QUEUE, 'tasks/queue');
});

test('TASKS_PAUSE is defined and is a string', () => {
  assert.equal(typeof API.TASKS_PAUSE, 'string');
  assert.equal(API.TASKS_PAUSE, 'tasks/pause');
});

test('TASKS_RESUME is defined and is a string', () => {
  assert.equal(typeof API.TASKS_RESUME, 'string');
  assert.equal(API.TASKS_RESUME, 'tasks/resume');
});

test('TASKS_RESUME_PENDING is defined and is a string', () => {
  assert.equal(typeof API.TASKS_RESUME_PENDING, 'string');
  assert.equal(API.TASKS_RESUME_PENDING, 'tasks/resume-pending');
});

test('TASKS_INTERRUPT is defined and is a string', () => {
  assert.equal(typeof API.TASKS_INTERRUPT, 'string');
  assert.equal(API.TASKS_INTERRUPT, 'tasks/interrupt');
});

test('TASKS_UNDO is defined and is a string', () => {
  assert.equal(typeof API.TASKS_UNDO, 'string');
  assert.equal(API.TASKS_UNDO, 'tasks/undo');
});

test('TASKS_OPS is defined and is a string', () => {
  assert.equal(typeof API.TASKS_OPS, 'string');
  assert.equal(API.TASKS_OPS, 'tasks/ops');
});

test('no TASKS.* object access pattern exists (regression guard)', () => {
  // TASKS should be a plain string, not an object with sub-paths
  assert.equal(typeof API.TASKS, 'string',
    'API.TASKS must be a string path, not an object');
  // Verify TASKS does NOT have sub-properties like PAUSE, RESUME, etc.
  assert.equal(API.TASKS.PAUSE, undefined,
    'API.TASKS.PAUSE must not exist (use API.TASKS_PAUSE)');
  assert.equal(API.TASKS.RESUME, undefined,
    'API.TASKS.RESUME must not exist (use API.TASKS_RESUME)');
  assert.equal(API.TASKS.INTERRUPT, undefined,
    'API.TASKS.INTERRUPT must not exist (use API.TASKS_INTERRUPT)');
  assert.equal(API.TASKS.UNDO, undefined,
    'API.TASKS.UNDO must not exist (use API.TASKS_UNDO)');
  assert.equal(API.TASKS.OPS, undefined,
    'API.TASKS.OPS must not exist (use API.TASKS_OPS)');
});

test('FILES.FOLDER_CREATE is defined and matches backend', () => {
  assert.equal(typeof API.FILES, 'object');
  assert.equal(API.FILES.FOLDER_CREATE, 'files/folder-create');
});

test('CONFIG_GET and CONFIG_SAVE are defined', () => {
  assert.equal(API.CONFIG_GET, 'config/get');
  assert.equal(API.CONFIG_SAVE, 'config/save');
});

test('SYNC_WITHERING and SYNC_STATUS are defined', () => {
  assert.equal(API.SYNC_WITHERING, 'sync/withering');
  assert.equal(API.SYNC_STATUS, 'sync/status');
});

test('all top-level API keys are non-empty strings or objects', () => {
  for (const [key, val] of Object.entries(API)) {
    if (typeof val === 'string') {
      assert.ok(val.length > 0, `API.${key} must not be empty string`);
    } else if (typeof val === 'object' && val !== null) {
      // Nested object like GROUPS, FILES, etc.
      for (const [subKey, subVal] of Object.entries(val)) {
        assert.equal(typeof subVal, 'string',
          `API.${key}.${subKey} must be a string`);
        assert.ok(subVal.length > 0,
          `API.${key}.${subKey} must not be empty string`);
      }
    }
  }
});
