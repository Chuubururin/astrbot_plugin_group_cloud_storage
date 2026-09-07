/**
 * Unit tests: resilient SSE client (I5 heartbeat watchdog, exponential
 * backoff redial, single-recovery callback).
 *
 * Timing constants are injected via options.timings (real timers with
 * short values) so the watchdog behavior is observable without waiting
 * the production 90s heartbeat window.
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';

const { createResilientSSE } = await import('../../utils/sse.js');
const { EVENT_TYPES } = await import('../../constants.js');

// Short timings: heartbeat window 40ms, backoff base 10ms, cap 50ms.
const T = { heartbeatTimeoutMs: 40, baseMs: 10, maxMs: 50 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Emitter-backed bridge stub matching the REAL host contract
 * (plugin_page_bridge.js, 2026-09-03): subscribeSSE(endpoint, handlers)
 * returns a Promise<subscriptionId>; events arrive via handlers.onMessage
 * with {raw, parsed}. api.js wraps this into the cancel-function contract
 * used by sse.js — the tests exercise the full sse.js → api.js → host chain.
 */
function makeClient(opts = {}) {
  const emitter = new EventEmitter();
  let subscriptions = 0;
  const sse = createResilientSSE({
    onEvent: opts.onEvent || (() => {}),
    onConnectionChange: opts.onConnectionChange || (() => {}),
    onReconnected: opts.onReconnected || (() => {}),
    timings: opts.timings || T,
  });
  globalThis.window = {
    AstrBotPluginPage: {
      subscribeSSE: (path, handlers) => {
        subscriptions += 1;
        const id = `sub_${subscriptions}`;
        emitter.on('evt', (obj) => {
          handlers.onMessage({ raw: JSON.stringify(obj), parsed: obj });
        });
        return Promise.resolve(id);
      },
      unsubscribeSSE: () => {},
    },
  };
  return { sse, emitter, count: () => subscriptions };
}

test('sse: heartbeats keep the connection healthy, real events flow', async () => {
  const seen = [];
  const changes = [];
  const { sse, emitter } = makeClient({ onEvent: (ev) => seen.push(ev.kind), onConnectionChange: (up) => changes.push(up) });
  sse.start();
  await sleep(10);  // initial subscribe settles
  for (let i = 0; i < 3; i++) {
    emitter.emit('evt', { type: EVENT_TYPES.HEARTBEAT });
    await sleep(20);  // inside the 40ms window
  }
  emitter.emit('evt', { type: EVENT_TYPES.DONE, kind: 'upload' });
  await sleep(20);
  assert.deepEqual(seen, ['upload'], 'heartbeats filtered, real events delivered');
  assert.deepEqual(changes, [], 'never degraded while heartbeats arrive');
  sse.stop();
});

test('sse: watchdog degrades after silence and recovers exactly once', async () => {
  const changes = [];
  let reconnected = 0;
  const { sse, emitter } = makeClient({
    onConnectionChange: (up) => changes.push(up),
    onReconnected: () => { reconnected += 1; },
  });
  sse.start();
  await sleep(10);

  // Silence past the heartbeat window -> degraded + redial scheduled.
  await sleep(T.heartbeatTimeoutMs + 15);
  assert.deepEqual(changes, [false], 'connection marked down');

  // Redial re-subscribes; the first received event marks recovery once.
  await sleep(T.baseMs + 15);
  emitter.emit('evt', { type: EVENT_TYPES.HEARTBEAT });
  emitter.emit('evt', { type: EVENT_TYPES.PROGRESS, kind: 'scan' });
  await sleep(15);

  assert.deepEqual(changes, [false, true], 'recovered after degraded');
  assert.equal(reconnected, 1, 'onReconnected fired exactly once');
  sse.stop();
});

test('sse: repeated timeouts keep redialing with capped backoff', async () => {
  const { sse, count } = makeClient();
  sse.start();
  await sleep(10);
  const initial = count();

  // A few watchdog trips: each schedules a redial with backoff (capped).
  await sleep(T.heartbeatTimeoutMs + T.maxMs + 20);
  assert.ok(count() > initial, 'redial re-subscribed after degradation');
  sse.stop();
});