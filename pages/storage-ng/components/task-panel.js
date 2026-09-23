/**
 * Task panel (A3/G1-G3) - floating log behind the header queue indicator.
 *
 * Renders the SSE-fed taskLog (newest first) through the shared keyed-row diff
 * (utils/dom-diff): rows reconcile by log id, an unchanged row keeps its node,
 * and a changed one is rebuilt by buildRow. Panel visibility follows
 * store.taskPanelOpen, toggled by cs-header.
 *
 * @module components/task-panel
 */

import { getState, subscribe, set } from '../store.js';
import { getIcon } from '../icons.js';
import { escapeHtml, formatTimeFull } from '../utils/helpers.js';
import { applyKeyedDiff } from '../utils/dom-diff.js';
import { STATE_CLASS, taskLogDetail, taskLogSignature } from '../views/task-labels.js';

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

  // 与所有其它列表共用同一个键控 diff 引擎：pushTaskLog 头插（最新在前），
  // want 顺序即渲染顺序，新行落在列表头部而不是追加到底部。
  // log_id = `${ts}-${task_id}-${type}`，同一毫秒的同类事件会撞 key：diff 加
  // 序号消歧并告警（此前手写的 Map 归并会把撞 key 的一行悄悄丢掉）。
  applyKeyedDiff(list, tasks, buildRow, (t) => String(t.log_id), taskLogSignature);
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
    <span class="task-detail">${escapeHtml(taskLogDetail(t))}</span>
  `;
  return el;
}
