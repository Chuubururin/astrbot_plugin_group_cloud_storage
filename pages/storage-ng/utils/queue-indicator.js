/**
 * Queue depth indicator.
 *
 * The SSE stream carries per-task progress only, never the global pending
 * depth; the header/status-bar queue readouts consume store.queueStatus,
 * which this poller maintains (2s interval; the endpoint reads in-memory
 * queue state, so the poll is cheap). The interval handle is returned so
 * the shell can stop the poll on unload, and an unchanged reading is not
 * re-published (subscribers repaint only on a real depth change).
 *
 * @module utils/queue-indicator
 */

import { apiGet, API } from '../api.js';
import { set } from '../store.js';
import { QUEUE_POLL_INTERVAL } from '../constants.js';

/** Stable signature of one reading (running is an array, never identical). */
function signature(pending, running) {
  try { return `${pending}|${JSON.stringify(running)}`; } catch (e) { return `${pending}|?`; }
}

/**
 * Start the queue-depth poll.
 * @returns {function} stop - clears the interval (idempotent)
 */
export function startQueueIndicator() {
  let lastSig = null;
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      const st = await apiGet(API.TASKS_QUEUE);
      // 后端 queue.status() 的字段是 depth/running；组件读 pending。
      const next = { pending: st?.depth ?? 0, running: st?.running ?? [] };
      const sig = signature(next.pending, next.running);
      if (sig === lastSig) return; // 深度未变：不通知订阅者重绘
      lastSig = sig;
      set('queueStatus', next);
    } catch {
      /* 指示器失败静默：展示层回退到 '-'，下个周期重试 */
    }
  };
  tick();
  const timer = setInterval(tick, QUEUE_POLL_INTERVAL);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
