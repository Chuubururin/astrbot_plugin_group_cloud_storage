/**
 * Queue depth indicator.
 *
 * The SSE stream carries per-task progress only, never the global pending
 * depth; the header/status-bar queue readouts consume store.queueStatus,
 * which this poller maintains (2s interval; the endpoint reads in-memory
 * queue state, so the poll is cheap).
 *
 * @module utils/queue-indicator
 */

import { apiGet, API } from '../api.js';
import { set } from '../store.js';
import { QUEUE_POLL_INTERVAL } from '../constants.js';

export function startQueueIndicator() {
  const tick = async () => {
    try {
      const st = await apiGet(API.TASKS_QUEUE);
      // 后端 queue.status() 的字段是 depth/running；组件读 pending。
      set('queueStatus', { pending: st?.depth ?? 0, running: st?.running ?? [] });
    } catch {
      /* 指示器失败静默：展示层回退到 '-'，下个周期重试 */
    }
  };
  tick();
  setInterval(tick, QUEUE_POLL_INTERVAL);
}
