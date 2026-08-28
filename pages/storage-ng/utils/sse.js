/**
 * SSE client - resilient event stream (I5: heartbeat watchdog).
 *
 * The backend emits heartbeats; if none arrives within the timeout the
 * connection is presumed dead and redialled with exponential backoff.
 * After a degraded period the first received event marks recovery and
 * fires onReconnected exactly once, letting main.js refresh all topics
 * (B-class state-driven recovery, zero redundant traffic while healthy).
 *
 * @module utils/sse
 */

import { subscribeSSE } from '../api.js';
import { EVENT_TYPES, SSE_HEARTBEAT_TIMEOUT_MS, SSE_RECONNECT_BASE_MS, SSE_RECONNECT_MAX_MS } from '../constants.js';

/**
 * Create a resilient SSE subscription.
 *
 * @param {Object} options
 * @param {function} options.onEvent - handler for non-heartbeat events
 * @param {function} [options.onConnectionChange] - (connected: boolean)
 * @param {function} [options.onReconnected] - fired once after recovery
 * @param {Object} [options.timings] - overrides for testability:
 *        {heartbeatTimeoutMs, baseMs, maxMs} (defaults from constants)
 * @returns {{start: function, stop: function, isConnected: function}}
 */
export function createResilientSSE(options = {}) {
  const { onEvent, onConnectionChange, onReconnected } = options;
  const timings = {
    heartbeatTimeoutMs: SSE_HEARTBEAT_TIMEOUT_MS,
    baseMs: SSE_RECONNECT_BASE_MS,
    maxMs: SSE_RECONNECT_MAX_MS,
    ...(options.timings || {}),
  };

  let unsub = null;
  let watchdog = null;
  let redialTimer = null;
  let backoffMs = timings.baseMs;
  let degraded = false;
  let stopped = true;

  function armWatchdog() {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      degraded = true;
      if (onConnectionChange) onConnectionChange(false);
      resubscribe(true);
    }, timings.heartbeatTimeoutMs);
  }

  function teardown() {
    clearTimeout(watchdog);
    watchdog = null;
    if (unsub) {
      try { unsub(); } catch (e) { /* listener already gone */ }
      unsub = null;
    }
  }

  function resubscribe(afterTimeout) {
    teardown();
    const delay = afterTimeout ? backoffMs : 0;
    backoffMs = Math.min(backoffMs * 2, timings.maxMs);
    redialTimer = setTimeout(() => {
      if (stopped) return;
      try {
        unsub = subscribeSSE(handleEvent);
      } catch (e) {
        console.error('[sse] subscribe failed, retrying:', e);
        resubscribe(true);
        return;
      }
      armWatchdog();
    }, delay);
  }

  function handleEvent(ev) {
    backoffMs = timings.baseMs;
    if (degraded) {
      degraded = false;
      if (onConnectionChange) onConnectionChange(true);
      if (onReconnected) onReconnected();
    }
    armWatchdog();
    if (!ev || ev.type === EVENT_TYPES.HEARTBEAT) return;
    onEvent(ev);
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      resubscribe(false);
    },
    stop() {
      stopped = true;
      clearTimeout(redialTimer);
      teardown();
      if (degraded) {
        degraded = false;
        if (onConnectionChange) onConnectionChange(true);
      }
    },
    isConnected() {
      return !degraded;
    },
  };
}