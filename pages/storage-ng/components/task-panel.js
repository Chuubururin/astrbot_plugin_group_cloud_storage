/**
 * Task panel (A3/G1-G3) - floating log behind the header queue indicator.
 *
 * Renders the SSE-fed taskLog (newest first); rows update in place by
 * log id so progress events mutate rather than rebuild (FE-14). Panel
 * visibility follows store.taskPanelOpen, toggled by cs-header.
 *
 * @module components/task-panel
 */

import { getState, subscribe, set } from '../store.js';
import { getIcon } from '../icons.js';
import { escapeHtml, formatTimeFull } from '../utils/helpers.js';

const STATE_CLASS = {
  queued: 'st-pending',
  started: 'st-running',
  progress: 'st-running',
  retry: 'st-running',
  done: 'st-done',
  failed: 'st-failed',
};

/** Initialize the task panel (singleton appended to body). */
export function initTaskPanel() {
  if (document.getElementById('task-panel')) return;

  const panel = document.createElement('aside');
  panel.id = 'task-panel';
  panel.className = 'task-panel hidden';
  panel.innerHTML = `
    <div class="task-panel-head">
      <span class="task-panel-title">${getIcon('MENU', 13)} 任务面板</span>
      <span id="task-panel-count" class="task-panel-count"></span>
      <button id="task-panel-clear" class="task-panel-toggle" title="清空任务日志">清空</button>
      <button id="task-panel-toggle" class="task-panel-toggle">收起</button>
    </div>
    <div id="task-panel-list" class="task-panel-list"></div>
  `;
  document.body.appendChild(panel);

  panel.querySelector('#task-panel-toggle')?.addEventListener('click', () => {
    set('taskPanelOpen', !getState().taskPanelOpen);
  });
  panel.querySelector('#task-panel-clear')?.addEventListener('click', async () => {
    const { clearTaskLog } = await import('../store.js');
    clearTaskLog();
  });

  subscribe('taskPanelOpen', (open) => {
    panel.classList.toggle('hidden', !open);
    const btn = panel.querySelector('#task-panel-toggle');
    if (btn) btn.textContent = open ? '收起' : '展开';
  });
  subscribe('taskLog', () => renderTasks(panel));
  renderTasks(panel);
}

function renderTasks(panel) {
  const list = panel.querySelector('#task-panel-list');
  const count = panel.querySelector('#task-panel-count');
  if (!list) return;

  const tasks = getState().taskLog || [];
  if (count) count.textContent = tasks.length ? `${tasks.length} 条` : '';

  // In-place update by log id (FE-14): remove stale, update or append.
  const existing = new Map();
  for (const el of Array.from(list.children)) {
    if (el.dataset && el.dataset.key != null) existing.set(el.dataset.key, el);
  }
  const wantSet = new Set(tasks.map((t) => t.log_id));
  for (const [key, el] of existing) {
    if (!wantSet.has(key)) el.remove();
  }
  for (const t of tasks) {
    let el = existing.get(t.log_id);
    if (el) updateRow(el, t);
    else list.appendChild(buildRow(t));
  }
}

function buildRow(t) {
  const el = document.createElement('div');
  el.className = 'task-row';
  el.dataset.key = t.log_id;
  el.dataset.taskId = t.task_id || '';
  el.innerHTML = `
    <span class="task-time">${formatTimeFull(t.ts / 1000)}</span>
    <span class="task-kind">${escapeHtml(t.kind || '-')}</span>
    <span class="task-state ${STATE_CLASS[t.type] || ''}">${escapeHtml(t.type || '-')}</span>
    <span class="task-detail">${escapeHtml(taskDetail(t))}</span>
  `;
  return el;
}

function updateRow(el, t) {
  const state = el.querySelector('.task-state');
  const detail = el.querySelector('.task-detail');
  if (state) {
    state.className = `task-state ${STATE_CLASS[t.type] || ''}`;
    state.textContent = t.type || '-';
  }
  if (detail) detail.textContent = taskDetail(t);
}

function taskDetail(t) {
  if (t.percent != null && t.percent > 0) {
    return `${t.detail || ''} ${Math.round(t.percent)}%`.trim();
  }
  return t.detail || t.state || '';
}