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
      // 只在状态跃迁时通知：连续两次看门狗超时（第一次重连后仍无心跳）
      // 不是新的「连接断开」事件，重复 false 会让订阅方收到噪声状态。
      if (!degraded) {
        degraded = true;
        if (onConnectionChange) onConnectionChange(false);
      }
      resubscribe(true);
    }, timings.heartbeatTimeoutMs);
  }

  /** Drop the current bridge subscription (cancel wrapper). */
  function release() {
    if (unsub) {
      try { unsub(); } catch (e) { /* listener already gone */ }
      unsub = null;
    }
  }

  function teardown() {
    clearTimeout(watchdog);
    watchdog = null;
    release();
  }

  function resubscribe(afterTimeout) {
    // 旧订阅在退避窗口内保持挂载：先 teardown 会立刻 cancel 旧包装器，
    // 退避期间到达的事件会被静默丢弃（慢死的连接其实还活着，恢复事件会漏）。
    // 新订阅建立后再释放旧的，消除这段无覆盖窗口。
    clearTimeout(watchdog);
    watchdog = null;
    const delay = afterTimeout ? backoffMs : 0;
    // 只有看门狗触发的重连才推进退避档位。start() 的首次订阅若也翻倍，
    // 首次重连会直接跳到 2×base（1s 档永不出现，实际序列 2/4/8/16/30s）。
    if (afterTimeout) backoffMs = Math.min(backoffMs * 2, timings.maxMs);
    clearTimeout(redialTimer);
    redialTimer = setTimeout(() => {
      if (stopped) return;
      const prev = unsub;
      try {
        unsub = subscribeSSE(handleEvent, () => {
          // Bridge-side channel error: reconnect immediately instead of
          // idling up to a full heartbeat timeout. The watchdog stays armed
          // as the fallback for silent deaths that never fire onError.
          resubscribe(true);
        });
      } catch (e) {
        console.error('[sse] subscribe failed, retrying:', e);
        resubscribe(true);
        return;
      }
      if (prev) { try { prev(); } catch (e) { /* already gone */ } }
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