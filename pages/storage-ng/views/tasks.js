/**
 * Tasks view (T-5) - task ledger + four actions + operation history.
 *
 * Shows the OpQueue ledger with a state filter and per-row controls:
 * continue (resume), pause, interrupt, undo (reversible-matrix only -
 * irreversible cloud deletes are explicitly surfaced, never faked), plus
 * the operation-history (ops) viewer. Scheduled scans are paused/resumed
 * from the toolbar. Plugin configuration lives in its own tab (config), not here.
 *
 * @module views/tasks
 */

import { getState, set, subscribe, refresh } from '../store.js';
import { API, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { formatTimeFull, escapeHtml } from '../utils/helpers.js';
import { confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';

/** 可撤销操作类型（与 task_control._REVERSIBLE_KINDS 对齐）。 */
const REVERSIBLE_KINDS = new Set(['move_file', 'replace_name', 'tags']);

const STATE_FILTERS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
];

const STATE_LABEL = {
  pending: '排队中',
  running: '运行中',
  paused: '已暂停',
  retry: '重试中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

const STATE_CLASS = {
  pending: 'st-pending',
  running: 'st-running',
  paused: 'st-paused',
  retry: 'st-running',
  done: 'st-done',
  failed: 'st-failed',
  cancelled: 'st-failed',
};

/** 人类可读的 kind 标签。 */
const KIND_LABEL = {
  move_file: '移动文件',
  replace_name: '改名',
  delete: '删除',
  file_scan: '文件扫描',
  diff_file_scan: '差分扫描',
  convert_volumes: '转分卷',
  video_upload: '视频上传',
  video_album: '视频相册',
  image_album: '图片相册',
  fetch: '抓取',
  essence_save: '精华入库',
  essence_delete: '精华删除',
  netdisk_index: '网盘索引',
  tags: '标签',
};

/**
 * Initialize the tasks view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initTasksView(container) {
  container.innerHTML = `
    <div class="tasks-toolbar toolbar">
      <div class="toolbar-left">
        <select id="task-state-filter">${STATE_FILTERS.map((f) =>
          `<option value="${f.value}">${f.label}</option>`).join('')}</select>
        <button id="task-refresh" class="icon-btn" title="刷新台账">${getIcon('REFRESH', 14)}</button>
      </div>
      <div class="toolbar-right">
        <span id="task-count" class="count-badge"></span>
      </div>
    </div>
    <div class="table-wrap">
      <table id="task-table">
        <thead><tr>
          <th>类型</th><th>目标</th><th>状态</th>
          <th>详情</th><th>时间</th><th>操作</th>
        </tr></thead>
        <tbody id="task-tbody"></tbody>
      </table>
    </div>
    <div id="ops-panel" class="ops-panel hidden"></div>
  `;

  loadTasks();

  const subs = [
    subscribe('refresh:tasks', loadTasks),
    subscribe('taskStateFilter', loadTasks),
  ];

  container.querySelector('#task-state-filter')?.addEventListener('change', (e) => {
    set('taskStateFilter', e.target.value);
  });
  container.querySelector('#task-refresh')?.addEventListener('click', loadTasks);

  return () => { subs.forEach((u) => u()); };
}

async function loadTasks() {
  set('loading', true);
  try {
    const state = getState().taskStateFilter || '';
    const params = {};
    if (state) params.state = state;
    const data = await apiPost(API.TASKS, params);
    set('taskLedger', data.tasks || []);
    renderTasks(data.tasks || []);
  } catch (e) {
    console.error('[tasks] load failed:', e);
    toast('加载任务台账失败', 'error');
  } finally {
    set('loading', false);
  }
}

/** 从 task 构建人类可读的详情摘要。 */
function taskSummary(t) {
  const p = t.payload || {};
  const kind = t.kind || '';
  if (kind === 'move_file') {
    const from = p.from_folder || p.old_folder || '';
    const to = p.to_folder || p.folder || '';
    if (from || to) return `${from || '?'} → ${to || '?'}${p.name ? ` (${p.name})` : ''}`;
  }
  if (kind === 'replace_name') {
    const old = p.old_name || p.name || '';
    const nw = p.new_name || '';
    if (old || nw) return `${old} → ${nw || '?'}`;
  }
  if (kind === 'delete') return p.name || p.ids?.length ? `${p.ids?.length || 1} 个文件` : '';
  if (kind === 'tags') {
    const tags = p.tags || p.after?.tags || [];
    return `标签: ${tags.join(', ') || '-'}`;
  }
  if (kind === 'fetch') return p.name || p.url || '';
  if (kind === 'essence_save') return p.title || '';
  if (kind === 'file_scan' || kind === 'diff_file_scan') {
    const groups = p.groups || [];
    return groups.length ? `${groups.length} 个群` : '全群扫描';
  }
  if (kind === 'convert_volumes') return p.name || '';
  if (kind === 'video_upload' || kind === 'video_album' || kind === 'image_album') return p.name || '';
  if (kind === 'netdisk_index') return p.path || '';
  return '';
}

function renderTasks(tasks) {
  const tbody = document.getElementById('task-tbody');
  if (!tbody) return;
  const count = document.getElementById('task-count');
  if (count) count.textContent = `${tasks.length} 条任务`;
  tbody.innerHTML = '';
  if (tasks.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="6" class="empty-hint">暂无任务</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const t of tasks) {
    const tr = document.createElement('tr');
    tr.dataset.key = t.task_id;
    const isDone = t.state === 'done';
    const isReversible = isDone && REVERSIBLE_KINDS.has(t.kind);
    const isTerminal = isDone || t.state === 'failed' || t.state === 'cancelled';
    const summary = taskSummary(t);
    const kindLabel = KIND_LABEL[t.kind] || t.kind || '-';
    const stateLabel = STATE_LABEL[t.state] || t.state || '-';
    tr.innerHTML = `
      <td title="${escapeHtml(t.kind || '')}">${escapeHtml(kindLabel)}</td>
      <td title="${escapeHtml(t.target || '')}">${escapeHtml(String(t.target || '-').slice(0, 24))}</td>
      <td><span class="badge ${STATE_CLASS[t.state] || ''}">${escapeHtml(stateLabel)}</span></td>
      <td class="task-detail" title="${escapeHtml(summary || t.error || '')}">${
        t.error && isTerminal
          ? `<span class="task-error">${escapeHtml(String(t.error).slice(0, 60))}</span>`
          : escapeHtml(String(summary || '-').slice(0, 60))
      }</td>
      <td>${formatTimeFull(t.created_at)}</td>
      <td class="task-actions">
        ${t.state === 'paused' ? `<button class="btn-act" data-act="resume" data-id="${t.task_id}">继续</button>` : ''}
        ${t.state === 'pending' || t.state === 'running' ? `<button class="btn-act" data-act="pause" data-id="${t.task_id}">暂停</button>` : ''}
        ${t.state === 'pending' || t.state === 'running' || t.state === 'paused'
          ? `<button class="btn-act danger" data-act="interrupt" data-id="${t.task_id}">中断</button>` : ''}
        ${isReversible
          ? `<button class="btn-act primary" data-act="undo" data-id="${t.task_id}" title="撤销已完成的操作（仅 移动/改名/标签 支持）">撤销</button>`
          : ''}
        <button class="btn-act" data-act="ops" data-id="${t.task_id}" title="查看操作记录">记录</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('[data-act]').forEach((btn) => {
    btn.addEventListener('click', () => handleAction(btn.dataset.act, btn.dataset.id));
  });
}

async function handleAction(act, taskId) {
  try {
    switch (act) {
      case 'pause':
        await apiPost(API.TASKS_PAUSE, { task_id: taskId });
        toast('已暂停（运行中为协作式，下一检查点生效）', 'success');
        break;
      case 'resume':
        await apiPost(API.TASKS_RESUME, { task_id: taskId });
        toast('已继续', 'success');
        break;
      case 'interrupt': {
        const ok = await confirmEx('中断任务', `确定中断任务？`, { danger: true });
        if (!ok) return;
        await apiPost(API.TASKS_INTERRUPT, { task_id: taskId });
        toast('已中断', 'success');
        break;
      }
      case 'undo': {
        const r = await apiPost(API.TASKS_UNDO, { task_id: taskId });
        if (r?.ok) {
          toast(`${r.note || '已撤销'}${r.compensation_task_id ? `（补偿任务已创建）` : ''}`, 'success');
        } else {
          toast(r?.reason || '不可撤销', r?.undoable === false ? 'warn' : 'error');
        }
        break;
      }
      case 'ops': {
        const r = await apiPost(API.TASKS_OPS, { task_id: taskId });
        showOps(taskId, r.ops || []);
        break;
      }
    }
    loadTasks();
  } catch (e) {
    toast(`操作失败: ${e.message || e}`, 'error');
  }
}

/** Operation-history viewer: shows before -> after transitions for a task. */
function showOps(taskId, ops) {
  const panel = document.getElementById('ops-panel');
  if (!panel) return;
  // Always show panel (even when empty) so user sees feedback
  panel.classList.remove('hidden');
  panel.innerHTML = `
    <div class="ops-head">
      <span>操作记录: ${escapeHtml(String(taskId).slice(0, 12))}</span>
      <button class="ops-close">收起</button>
    </div>
    <div class="ops-body">
      ${ops.length === 0
        ? '<div class="empty-hint">该任务无操作记录（仅移动/改名任务会记录操作流）</div>'
        : ops.map((o, i) => `
        <div class="ops-row">
          <span class="ops-idx">#${i + 1}</span>
          <span class="ops-act">${escapeHtml(KIND_LABEL[o.action] || o.action || '-')}</span>
          <code class="ops-before">${escapeHtml(JSON.stringify(o.before || {}))}</code>
          <span class="ops-arrow">→</span>
          <code class="ops-after">${escapeHtml(JSON.stringify(o.after || {}))}</code>
        </div>`).join('')}
    </div>
  `;
  panel.querySelector('.ops-close')?.addEventListener('click', () => panel.classList.add('hidden'));
}
