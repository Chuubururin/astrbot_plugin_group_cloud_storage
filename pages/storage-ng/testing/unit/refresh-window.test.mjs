/**
 * Unit tests: refresh-window semantics.
 *
 * The trailing window is the request-side collapse for `data_changed` bursts:
 * repeats inside `delayMs` fold into one refresh, and `maxWaitMs` bounds how
 * long a stream arriving faster than the window may defer it - the same
 * contract as throttle's maxWait. The visibility net in main.js is not a
 * backstop for this, because it only evaluates when a tab comes back into view.
 *
 * Leading mode is the other contract: the event is the echo of a click, so the
 * first repaint must be immediate and only the repeats fold.
 *
 * Run: node --test pages/storage-ng/testing/unit/refresh-window.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  createRefreshWindow, COALESCE_WINDOW_MS, COALESCE_MAX_WAIT_MS,
} from '../../utils/refresh-coalescer.js';

/** Window writing to a publish log over fake timers. */
function harness(t, options = {}) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const published = [];
  const schedule = createRefreshWindow({
    publish: (topic) => published.push(topic), ...options,
  });
  return { published, schedule };
}

test('trailing: a single event publishes once, at the default window edge', (t) => {
  const { published, schedule } = harness(t);
  schedule('files');
  t.mock.timers.tick(COALESCE_WINDOW_MS - 1);
  assert.deepEqual(published, [], 'nothing while the window is open');
  t.mock.timers.tick(1);
  assert.deepEqual(published, ['files'], 'one publish when it closes');
  t.mock.timers.tick(COALESCE_MAX_WAIT_MS * 5);
  assert.equal(published.length, 1, 'and it never repeats on its own');
});

test('trailing: a burst inside one window collapses to a single publish', (t) => {
  const { published, schedule } = harness(t, { delayMs: 150, maxWaitMs: 1000 });
  for (let i = 0; i < 20; i += 1) {
    schedule('files');
    t.mock.timers.tick(5);            // 20 events over 100ms, inside one window
  }
  t.mock.timers.tick(200);
  assert.equal(published.length, 1, `20 events must not become 20 requests`);
});

test('trailing: a stream faster than the window still publishes (maxWait)', (t) => {
  const { published, schedule } = harness(t, { delayMs: 150, maxWaitMs: 1000 });
  const at = [];
  let last = 0;
  let elapsed = 0;
  // 5s of continuous events every 50ms, faster than the window.
  while (elapsed < 5000) {
    schedule('files');
    t.mock.timers.tick(50);
    elapsed += 50;
    if (published.length > last) { at.push(elapsed); last = published.length; }
  }
  assert.ok(at.length >= 4, `expected a publish roughly every maxWait, got ${at.length}`);
  for (const ms of at) assert.ok(ms >= 1000, `first publish at ${ms}ms preceded the cap`);
  const gaps = at.slice(1).map((ms, i) => ms - at[i]);
  assert.ok(gaps.every((g) => g <= 1050),
    `deferral must stay bounded by maxWait (+ one event step), got ${gaps.join(',')}`);
});

test('trailing: the tail of a stopped stream still gets its own refresh', (t) => {
  const { published, schedule } = harness(t, { delayMs: 150, maxWaitMs: 1000 });
  for (let i = 0; i < 21; i += 1) { schedule('files'); t.mock.timers.tick(50); }
  const during = published.length;
  t.mock.timers.tick(200);            // the stream stops: the open window closes
  assert.equal(published.length, during + 1,
    'the last events must not be swallowed by the forced publishes');
  t.mock.timers.tick(5000);
  assert.equal(published.length, during + 1, 'and nothing fires after it settles');
});

test('trailing: each topic keeps its own window', (t) => {
  const { published, schedule } = harness(t, { delayMs: 150, maxWaitMs: 1000 });
  schedule('files');
  t.mock.timers.tick(100);
  schedule('groups');
  t.mock.timers.tick(50);             // files window closes here
  assert.deepEqual(published, ['files'], "one topic's deadline must not publish another");
  t.mock.timers.tick(100);
  assert.deepEqual(published, ['files', 'groups']);
});

test('trailing: an array of topics publishes each of them', (t) => {
  const { published, schedule } = harness(t, { delayMs: 20, maxWaitMs: 100 });
  schedule(['files', 'bridge']);
  t.mock.timers.tick(30);
  assert.deepEqual(published, ['files', 'bridge']);
});

test('leading: the first event publishes immediately, repeats fold into one', (t) => {
  const { published, schedule } = harness(t, { leading: true, delayMs: 150 });
  schedule('tasks');
  assert.deepEqual(published, ['tasks'], 'a click echo repaints at once');
  for (let i = 0; i < 20; i += 1) { schedule('tasks'); t.mock.timers.tick(5); }
  assert.deepEqual(published, ['tasks'], 'the batch itself adds nothing');
  t.mock.timers.tick(200);
  assert.deepEqual(published, ['tasks', 'tasks'], 'one follow-up after the window');
});

test('leading: a sustained stream publishes once per window, never zero', (t) => {
  const { published, schedule } = harness(t, { leading: true, delayMs: 150 });
  for (let i = 0; i < 100; i += 1) { schedule('tasks'); t.mock.timers.tick(50); }
  assert.ok(published.length >= 30,
    `leading is a throttle: 5s of events must keep repainting, got ${published.length}`);
  assert.ok(published.length <= 40,
    `and no more than one publish per window, got ${published.length}`);
});
