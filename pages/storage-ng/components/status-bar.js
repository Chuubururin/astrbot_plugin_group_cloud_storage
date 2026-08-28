/**
 * Status bar - footer strip (A4): loading, group context, active task,
 * errors, SSE health and queue depth.
 *
 * @module components/status-bar
 */

import { subscribe } from '../store.js';
import { getIcon } from '../icons.js';

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
  subscribe('error', (err) => {
    const el = container.querySelector('#status-error');
    if (el) {
      el.textContent = err || '';
      el.classList.toggle('hidden', !err);
    }
  });
  subscribe('queueStatus', (status) => {
    const el = container.querySelector('#status-queue');
    if (el) {
      const pending = status?.pending || 0;
      el.textContent = pending > 0 ? `队列: ${pending}` : '';
    }
  });
}