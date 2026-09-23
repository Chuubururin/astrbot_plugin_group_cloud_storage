/**
 * Tasks view  - ledger + four actions + operation history.
 *
 * Filters/paging follow the backend contract (limit/offset, never page/page_size);
 * rows render through the keyed diff so SSE-driven reloads do not rebuild the
 * whole table. The toolbar surfaces sync/status and offers sync/withering
 * manually - no scheduler pause/resume endpoint exists, so scheduling is shown, never faked. @module views/tasks
 */

import { getState, set, subscribe, nextSeq, isStale } from '../store.js';
import { API, apiGet, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { formatTimeFull, escapeHtml } from '../utils/helpers.js';
import { applyKeyedDiff } from '../utils/dom-diff.js';
import { createCoalescedLoader } from '../utils/refresh-coalescer.js';
import { confirmEx } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { DEFAULT_PAGE_SIZE } from '../constants.js';
import {
  REVERSIBLE_KINDS, STATE_FILTERS, STATE_LABEL, STATE_CLASS, KIND_LABEL,
  taskSummary, taskSignature,
} from './task-labels.js';

/** 删除类云端操作不可逆（后端 task_control 撤销矩阵：删除类返回 undoable=false）。 */
const IRREVERSIBLE_KINDS = new Set(['delete', 'essence_delete']);
/** kind 筛选：机器值直接取 KIND_LABEL 全集（后端 tasks 支持 kind 过滤）。 */
const KIND_FILTERS = [{ value: '', label: '全部类型' },
  ...Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))];
/** {value,label} 表 -> <option> 标记。 */
const options = (filters) => filters.map((f) => `<option value="${f.value}">${f.label}</option>`).join('');

/** Initialize the tasks view. @returns {function} cleanup */
export function initTasksView(container) {
  container.innerHTML = `
    <div class="tasks-toolbar toolbar">
      <div class="toolbar-left">
        <select id="task-state-filter">${options(STATE_FILTERS)}</select>
        <select id="task-kind-filter">${options(KIND_FILTERS)}</select>
        <input type="search" id="task-target-filter" placeholder="目标群 ID" />
        <button id="task-refresh" class="icon-btn" title="刷新列表">${getIcon('REFRESH', 14)}</button>
        <button id="task-resume-pending" class="btn-act" title="重提重启后遗留的可恢复任务（预检失败的任务会直接落失败态）">断点恢复</button>
      </div>
      <div class="toolbar-right">
        <span id="task-sync-status" class="count-badge">定时对账状态加载中…</span>
        <button id="task-sync-run" class="btn-act" title="手动触发一次凋零差分对账（缺省 = 全部受管群）">手动对账</button>
        <span id="task-count" class="count-badge"></span>
      </div>
    </div>
    <div class="table-wrap">
      <table id="task-table">
        <thead><tr><th>类型</th><th>目标</th><th>状态</th><th>详情</th><th>时间</th><th>操作</th></tr></thead>
        <tbody id="task-tbody"></tbody>
      </table>
    </div>
    <div class="tasks-pager"><button id="task-page-prev" class="btn-act">上一页</button><button id="task-page-next" class="btn-act">下一页</button></div>
    <div id="ops-panel" class="ops-panel hidden"></div>
  `;

  // 回填持久筛选：每次切 Tab 都会销毁重建视图，select 必须按 store 恢复，否则下拉会错位。
  const st = getState();
  const q = (sel) => container.querySelector(sel);
  q('#task-state-filter').value = st.taskStateFilter || '';
  q('#task-kind-filter').value = st.taskKindFilter || '';
  q('#task-target-filter').value = st.taskTargetFilter || '';
  const subs = ['refresh:tasks', 'taskStateFilter', 'taskKindFilter', 'taskTargetFilter']
    .map((key) => subscribe(key, loadTasks));
  loadTasks(); loadSyncStatus();
  // 改筛选回到第 1 页（页码无订阅者，先落页码再由筛选变更触发一次加载）。
  const onFilter = (key) => (e) => { set('taskPage', 1); set(key, e.target.value); };
  const onPage = (delta) => () => {
    set('taskPage', Math.max(1, (getState().taskPage || 1) + delta));
    loadTasks();
  };
  const bind = (sel, ev, fn) => q(sel).addEventListener(ev, fn);
  bind('#task-state-filter', 'change', onFilter('taskStateFilter'));
  bind('#task-kind-filter', 'change', onFilter('taskKindFilter'));
  bind('#task-target-filter', 'change', onFilter('taskTargetFilter'));
  bind('#task-refresh', 'click', loadTasks);
  bind('#task-resume-pending', 'click', resumePending);
  bind('#task-sync-run', 'click', runWithering);
  bind('#task-page-prev', 'click', onPage(-1));
  bind('#task-page-next', 'click', onPage(1));
  return () => { subs.forEach((u) => u()); };
}

/** The ledger request itself; reloads go through `ledgerLoader` below. */
async function fetchTaskLedger() {
  const seq = nextSeq('tasks');
  set('loading', true);
  try {
    const st = getState();
    const limit = DEFAULT_PAGE_SIZE;
    const page = Math.max(1, st.taskPage || 1);
    const params = { limit, offset: (page - 1) * limit };
    if (st.taskStateFilter) params.state = st.taskStateFilter;
    if (st.taskKindFilter) params.kind = st.taskKindFilter;
    if (st.taskTargetFilter) params.target = st.taskTargetFilter;
    const data = await apiPost(API.TASKS, params);
    if (isStale('tasks', seq)) return; // superseded by a newer request
    const tasks = data.tasks || [];
    // 后端 total = 本页条数（webapi/tasks.py: total=len(tasks)），不是表内总数：
    // 计数按「本页」口径，下一页可用性由本页是否填满 limit 推断。
    set('taskLedger', tasks);
    renderTasks(tasks, { page, hasMore: tasks.length >= limit });
  } catch (e) {
    console.error('[tasks] load failed:', e);
    toast('加载任务记录失败', 'error');
  } finally {
    set('loading', false);
  }
}

// 账本重载的唯一合并入口（形状同 components/data-table.js 的 load()：在飞期间
// 到达的触发只标脏，落地后补一次；补刷重新读取 getState()，所以带的是**当前**
// 筛选/页码，不是触发时的旧值）。六个触发源——筛选订阅、分页、刷新按钮、
// 动作后置重载、SSE 队列事件、重连全量刷新——全部经此，一次批量取消不再是
// N 条并发 POST /tasks。
const ledgerLoader = createCoalescedLoader(fetchTaskLedger);

/** Ask for a ledger reload (coalesced; fire-and-forget like every caller). */
function loadTasks() { ledgerLoader.load(); }

/** Empty-state placeholder row; keyed 'empty' so a later diff releases it. */
function buildEmptyRow() {
  const tr = document.createElement('tr');
  tr.innerHTML = '<td colspan="6" class="empty-hint">暂无任务</td>';
  return tr;
}

function renderTasks(tasks, meta) {
  const tbody = document.getElementById('task-tbody');
  if (!tbody) return;
  const count = document.getElementById('task-count');
  if (count) count.textContent = `本页 ${tasks.length} 条 · 第 ${meta.page} 页`;
  const pageBtn = (id, off) => { const b = document.getElementById(id); if (b) b.disabled = off; };
  pageBtn('task-page-prev', meta.page <= 1);
  pageBtn('task-page-next', !meta.hasMore);
  // 键控 diff：暂停/继续/重试等 SSE 事件只重写变化的行，不再整表重建
  // （整表重建会闪烁并丢失滚动位置与焦点）。空态同样走 diff（占位 key='empty'）：
  // 直写 innerHTML 不会作废上一帧的 create 计划，先非空后空时旧行会残留。
  const empty = tasks.length === 0;
  applyKeyedDiff(tbody, empty ? [{ task_id: 'empty' }] : tasks,
    empty ? buildEmptyRow : buildTaskRow, (t) => String(t.task_id), taskSignature);
}

/** Build one ledger row (keyed diff reuses nodes, so listeners bind here). */
function buildTaskRow(t) {
  const tr = document.createElement('tr');
  tr.dataset.key = String(t.task_id);
  const isDone = t.state === 'done';
  const isTerminal = isDone || t.state === 'failed' || t.state === 'cancelled';
  const summary = taskSummary(t);
  tr.innerHTML = `
    <td title="${escapeHtml(t.kind || '')}">${escapeHtml(KIND_LABEL[t.kind] || t.kind || '-')}</td>
    <td title="${escapeHtml(t.target || '')}">${escapeHtml(String(t.target || '-').slice(0, 24))}</td>
    <td><span class="badge ${STATE_CLASS[t.state] || ''}">${escapeHtml(STATE_LABEL[t.state] || t.state || '-')}</span></td>
    <td class="task-detail" title="${escapeHtml(summary || t.error || '')}">${
      t.error && isTerminal
        ? `<span class="task-error">${escapeHtml(String(t.error).slice(0, 60))}</span>`
        : escapeHtml(String(summary || '-').slice(0, 60))
    }</td>
    <td>${formatTimeFull(t.created_at)}</td>
    <td class="task-actions">${actionCell(t, isDone)}</td>
  `;
  tr.querySelectorAll('[data-act]').forEach((b) =>
    b.addEventListener('click', () => handleAction(b.dataset.act, b.dataset.id)));
  return tr;
}

/** Row actions. Completed delete-type rows are labelled irreversible, not faked. */
function actionCell(t, isDone) {
  // task_id 直接进属性：必须转义，否则含引号的 id 会截断属性，按钮把错误
  // 的 id 发给 tasks/pause|undo|ops。
  const id = escapeHtml(String(t.task_id));
  const active = ['pending', 'running', 'retry'].includes(t.state);
  const btn = (act, label, cls = '', title = '') =>
    `<button class="btn-act${cls}" data-act="${act}" data-id="${id}"${title ? ` title="${title}"` : ''}>${label}</button>`;
  const undo = isDone && REVERSIBLE_KINDS.has(t.kind)
    ? btn('undo', '撤销', ' primary', '撤销已完成的操作（仅 移动/改名/标签 支持）')
    : (isDone && IRREVERSIBLE_KINDS.has(t.kind)
      ? '<span class="badge warn" title="删除类云端操作不可逆，无法撤销">不可撤销</span>' : '');
  return [
    t.state === 'paused' ? btn('resume', '继续') : '',
    active ? btn('pause', '暂停') : '',
    active || t.state === 'paused' ? btn('interrupt', '中断', ' danger') : '',
    undo,
    btn('ops', '记录', '', '查看操作记录'),
  ].join('');
}

/** 定时调度可见性：sync/status（后端 auto_scan_loop 直接入队，不走 HTTP）。 */
async function loadSyncStatus() {
  const el = document.getElementById('task-sync-status');
  if (!el) return;
  try {
    const s = await apiGet(API.SYNC_STATUS);
    const q = s?.queue || {}, last = s?.last_diff_scan, hours = Number(s?.auto_scan_hours) || 0;
    el.textContent = `定时对账 ${hours > 0 ? `每 ${hours} 小时` : '已关闭'} · 上次 ${
      last ? `${STATE_LABEL[last.state] || last.state} ${formatTimeFull(last.updated_at || last.created_at)}` : '无'
    } · 对账运行中 ${s?.running_count ?? 0} · 队列 ${q.depth ?? 0} 等待 / ${(q.running || []).length} 执行`;
  } catch (e) {
    el.textContent = '定时对账状态不可用';
  }
}

/** 手动触发凋零差分对账（sync/withering；空 body = 全部受管群）。 */
async function runWithering() {
  try {
    const r = await apiPost(API.SYNC_WITHERING, {});
    toast(r?.mode === 'all' ? '已提交全群凋零对账' : `已提交凋零对账（${r?.groups || 0} 个群）`, 'success');
    loadSyncStatus();
    loadTasks();
  } catch (e) {
    toast(`手动对账失败: ${e.message || e}`, 'error');
  }
}

/** 断点恢复：重提重启后遗留的可恢复 pending 任务（白名单 + 预检在后端）。 */
async function resumePending() {
  try {
    const r = await apiPost(API.TASKS_RESUME_PENDING, {});
    const preflight = r.failed_preflight ? `，${r.failed_preflight} 个预检失败已落失败态` : '';
    if (r.resumed > 0) {
      toast(`已重提 ${r.resumed} 个断点任务${r.already_queued ? `，${r.already_queued} 个在队未重复提交` : ''}${preflight}`, 'success');
    } else if (r.already_queued > 0) {
      // 在队的行按原身份认领、不再重复提交，也不能报成"已重提"
      toast(`${r.already_queued} 个断点任务仍在队列中，未重复提交${preflight}`, 'info');
    } else if (r.failed_preflight > 0) {
      toast(`${r.failed_preflight} 个断点任务预检失败（输入已不存在），已落失败态`, 'warn');
    } else {
      toast(r.note || '无待恢复任务', 'info');
    }
    loadTasks();
  } catch (e) {
    toast(`断点恢复失败: ${e.message || e}`, 'error');
  }
}

async function handleAction(act, taskId) {
  try {
    switch (act) {
      case 'pause': case 'resume': {
        const pause = act === 'pause';
        const r = pause
          ? await apiPost(API.TASKS_PAUSE, { task_id: taskId })
          : await apiPost(API.TASKS_RESUME, { task_id: taskId });
        if (!r?.ok) { toast(r?.reason || `${pause ? '暂停' : '继续'}失败（任务状态不允许）`, 'error'); break; }
        toast(pause ? '已暂停（运行中为协作式，下一检查点生效）' : '已继续', 'success');
        break;
      }
      case 'interrupt': {
        if (!await confirmEx('中断任务', `确定中断任务？`, { danger: true })) return;
        const r = await apiPost(API.TASKS_INTERRUPT, { task_id: taskId });
        if (!r?.ok) { toast(r?.reason || '中断失败（任务不存在或已终态）', 'error'); break; }
        toast('已中断', 'success');
        break;
      }
      case 'undo': {
        const r = await apiPost(API.TASKS_UNDO, { task_id: taskId });
        if (r?.ok) {
          toast(`${r.note || '已撤销'}${r.compensation_task_id ? '（补偿任务已创建）' : ''}`, 'success');
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
    // 点击后的这一次重载不能删：`ops` 不产生任何 SSE 事件、`undo` 只入队补偿任务
    // （QUEUED/DONE 不在队列事件白名单里）、SSE 断线时也没有回声——删了就再没人重画。
    // 它与 SSE 回声的重复由 ledgerLoader 合并（在飞只标脏），所以不再并发两条。
    loadTasks();
  } catch (e) {
    toast(`操作失败: ${e.message || e}`, 'error');
  }
}

/** Operation-history viewer: shows before -> after transitions for a task. */
function showOps(taskId, ops) {
  const panel = document.getElementById('ops-panel');
  if (!panel) return;
  panel.classList.remove('hidden'); // 空记录也展开：用户需要看到反馈
  const rows = ops.map((o, i) => '<div class="ops-row">'
    + `<span class="ops-idx">#${i + 1}</span>`
    + `<span class="ops-act">${escapeHtml(KIND_LABEL[o.action] || o.action || '-')}</span>`
    + `<code class="ops-before">${escapeHtml(JSON.stringify(o.before || {}))}</code><span class="ops-arrow">→</span>`
    + `<code class="ops-after">${escapeHtml(JSON.stringify(o.after || {}))}</code></div>`).join('');
  const empty = '<div class="empty-hint">该任务无操作记录（仅移动/改名任务会记录操作流）</div>';
  panel.innerHTML = `<div class="ops-head"><span>操作记录: ${
    escapeHtml(String(taskId).slice(0, 12))}</span><button class="ops-close">收起</button></div>`
    + `<div class="ops-body">${ops.length === 0 ? empty : rows}</div>`;
  panel.querySelector('.ops-close')?.addEventListener('click', () => panel.classList.add('hidden'));
}
