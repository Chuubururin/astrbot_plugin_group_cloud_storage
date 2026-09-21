/**
 * Status bar - footer strip (A4): loading, group context, active task,
 * errors, SSE health and queue depth.
 *
 * @module components/status-bar
 */

import { set, subscribe } from '../store.js';
import { getIcon } from '../icons.js';

// Footer error lifetime. A single local constant: only this module clears
// the error line (the store has no other writer or clearer for it).
const ERROR_CLEAR_MS = 8000;

/**
 * Initialize the status bar.
 * @param {HTMLElement} container - <footer id="status-bar">
 */
export function initStatusBar(container) {
  container.className = 'status-bar';
  container.innerHTML = `
    <span id="status-loading" class="status-item"></span>
    <span id="status-group" class="status-item"></span>
    <span id="status-task" class="status-item"></span>
    <span id="status-error" class="status-item status-error"></span>
    <span id="status-sse" class="status-item status-offline hidden"></span>
    <span class="spacer"></span>
    <span id="status-queue" class="status-item"></span>
  `;

  subscribe('loading', (loading) => {
    const el = container.querySelector('#status-loading');
    if (el) {
      el.innerHTML = loading ? `${getIcon('LOADING', 12)} 加载中...` : '就绪';
    }
  });
  subscribe('sseConnected', (connected) => {
    const el = container.querySelector('#status-sse');
    if (!el) return;
    el.innerHTML = connected ? '' : `${getIcon('ALERT', 12)} 连接断开，重连中`;
    el.classList.toggle('hidden', connected);
  });
  subscribe('currentGroup', (group) => {
    const el = container.querySelector('#status-group');
    if (el) el.textContent = group ? `群: ${group}` : '';
  });
  subscribe('activeTask', (task) => {
    const el = container.querySelector('#status-task');
    if (el) {
      el.textContent = task
        ? `${task.kind}: ${task.detail || ''} ${task.i}/${task.n}`
        : '';
    }
  });
  // 全仓只有 main.js 会写 error，此前没有任何清除路径：超时自动清除，
  // 点击错误行也可立即清除，否则一次旧错误会永久占住状态栏。
  let errorTimer = null;
  subscribe('error', (err) => {
    const el = container.querySelector('#status-error');
    if (el) {
      el.textContent = err || '';
      el.classList.toggle('hidden', !err);
    }
    clearTimeout(errorTimer);
    errorTimer = err ? setTimeout(() => set('error', null), ERROR_CLEAR_MS) : null;
  });
  container.querySelector('#status-error')?.addEventListener('click', () => set('error', null));
  subscribe('queueStatus', (status) => {
    const el = container.querySelector('#status-queue');
    if (el) {
      const pending = status?.pending || 0;
      el.textContent = pending > 0 ? `队列: ${pending}` : '';
    }
  });
}