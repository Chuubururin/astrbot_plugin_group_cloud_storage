/**
 * Unit tests: api.subscribeSSE host-bridge contract wrapper (2026-09-03).
 *
 * Regression: the host bridge expects a handlers OBJECT ({onMessage}) and
 * returns a subscriptionId; previously the raw handler was passed through
 * (no events ever delivered → watchdog always degraded → "连接断开，重连中"
 * forever) and the returned promise was called as a function (subscriptions
 * never cancelled, accumulating on every redial).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { subscribeSSE } from '../../api.js';

function withBridge(bridge, fn) {
  const prev = globalThis.window;
  globalThis.window = { AstrBotPluginPage: bridge, location: { origin: 'http://x' } };
  try {
    return fn();
  } finally {
    if (prev === undefined) delete globalThis.window;
    else globalThis.window = prev;
  }
}

test('api.sse: async host bridge delivers parsed events (onMessage contract)', async () => {
  const calls = [];
  let resolveSub;
  let captured = null;
  const bridge = {
    subscribeSSE(endpoint, handlers) {
      calls.push(['sub', endpoint]);
      captured = handlers;
      return new Promise((res) => { resolveSub = res; });
    },
    unsubscribeSSE(id) { calls.push(['unsub', id]); },
  };
  const seen = [];
  const cancel = withBridge(bridge, () => subscribeSSE((ev) => seen.push(ev.type)));

  assert.equal(typeof captured.onMessage, 'function');
  assert.equal(typeof captured.onError, 'function');
  // host pushes sse_message {raw, parsed}
  captured.onMessage({ raw: '{"type":"heartbeat"}', parsed: { type: 'heartbeat' } });
  captured.onMessage({ raw: '{"type":"done","task_id":"t1"}', parsed: { type: 'done', task_id: 't1' } });
  assert.deepEqual(seen, ['heartbeat', 'done'], 'parsed events delivered');

  resolveSub('plugin_sse_1');
  await Promise.resolve();
  cancel();
  assert.deepEqual(calls[1], ['unsub', 'plugin_sse_1'], 'cancel unsubscribes by id');
});

test('api.sse: cancel before subscription settles still unsubscribes', async () => {
  let resolveSub;
  const unsubs = [];
  const bridge = {
    subscribeSSE() {
      return new Promise((res) => { resolveSub = res; });
    },
    unsubscribeSSE(id) { unsubs.push(id); },
  };
  const cancel = withBridge(bridge, () => subscribeSSE(() => {}));
  cancel();  // before resolve
  resolveSub('plugin_sse_9');
  await Promise.resolve();
  assert.deepEqual(unsubs, ['plugin_sse_9'], 'late settle unsubscribes');
});

test('api.sse: synchronous cancel contract (E2E fetch adapter) passes through', () => {
  let unsubbed = false;
  const bridge = {
    subscribeSSE() { return () => { unsubbed = true; }; },
  };
  const cancel = withBridge(bridge, () => subscribeSSE(() => {}));
  cancel();
  assert.equal(unsubbed, true, 'sync cancel forwarded');
});

test('api.sse: no SDK -> no-op cancel', () => {
  const cancel = withBridge(null, () => subscribeSSE(() => {}));
  assert.equal(typeof cancel, 'function');
  cancel();  // must not throw
});

test('api.sse: handler errors are contained (one bad event does not break stream)', () => {
  let captured = null;
  const bridge = {
    subscribeSSE(endpoint, handlers) {
      captured = handlers;
      return new Promise(() => {});  // never settles; cancel is a no-op
    },
  };
  const orig = console.error;
  console.error = () => {};
  try {
    withBridge(bridge, () => subscribeSSE(() => { throw new Error('boom'); }));
    // 单个事件抛错不得向外冒泡（流继续）
    captured.onMessage({ parsed: { type: 'done' } });
  } finally {
    console.error = orig;
  }
  assert.ok(true, 'handler exception contained');
});