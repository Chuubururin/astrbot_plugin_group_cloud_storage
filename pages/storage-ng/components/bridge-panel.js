/**
 * Bridge panel - OpenList transfer task management (T-4, merged from the
 * former standalone bridge view).
 *
 * Renders the bridge status strip, the transfer-task table with
 * direction tabs (group -> netdisk and netdisk -> group), retry/cancel
 * per task, and the bridge connection configuration modal.
 *
 * @module components/bridge-panel
 */

import { getState, set, subscribe, refresh } from '../store.js';
import { API, apiGet, apiPost, BRIDGE_STATE_LABELS } from '../api.js';
import { BRIDGE_STATES } from '../constants.js';
import { getIcon } from '../icons.js';
import { formatTimeFull, escapeHtml } from '../utils/helpers.js';
import { showFormModal } from './modal.js';
import { toast } from './toast.js';

const CONFIG_FIELDS = [
  { name: 'openlist_base_url', label: 'OpenList 地址', placeholder: 'http://host:5244' },
  { name: 'openlist_username', label: '用户名', placeholder: 'admin' },
  { name: 'openlist_password', label: '密码', type: 'password', placeholder: '密码' },
  { name: 'openlist_dst_dir', label: '目标目录', placeholder: '/smb' },
  { name: 'openlist_allow_private_address', label: '允许私有地址', type: 'select',
    options: [{ value: 'false', label: '否' }, { value: 'true', label: '是' }] },
];

/**
 * Initialize the bridge panel (status + transfer tasks).
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initBridgePanel(container) {
  container.innerHTML = `
    <div class="bridge-header">
      <h2>桥接传输</h2>
      <div class="bridge-status" id="bridge-status"></div>
    </div>
    <div class="bridge-toolbar">
      <button id="btn-bridge-refresh">${getIcon('REFRESH', 13)} 刷新</button>
      <button id="btn-bridge-config">${getIcon('SETTINGS', 13)} 配置</button>
    </div>
    <div class="bridge-tabs">
      <button class="tab-btn active" data-direction="out">转存到网盘</button>
      <button class="tab-btn" data-direction="in">转存到群</button>
    </div>
    <div class="table-wrap">
      <table class="compact bridge-table">
        <thead><tr>
          <th>任务ID</th><th>资源</th><th>路径</th><th>状态</th><th>更新时间</th><th>操作</th>
        </tr></thead>
        <tbody id="bridge-tbody"></tbody>
      </table>
    </div>
  `;

  loadBridgeStatus();
  loadBridgeTasks(getState().currentBridgeDirection || 'out');

  const unsubRefresh = subscribe('refresh:bridge', () => {
    loadBridgeStatus();
    loadBridgeTasks(getState().currentBridgeDirection || 'out');
  });

  container.querySelector('#btn-bridge-refresh')?.addEventListener('click', () => {
    loadBridgeStatus();
    loadBridgeTasks(getState().currentBridgeDirection || 'out');
  });
  container.querySelector('#btn-bridge-config')?.addEventListener('click', openBridgeConfig);
  container.querySelectorAll('.bridge-tabs .tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.bridge-tabs .tab-btn')
        .forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      set('currentBridgeDirection', btn.dataset.direction);
      loadBridgeTasks(btn.dataset.direction);
    });
  });

  return () => { unsubRefresh(); };
}

async function loadBridgeStatus() {
  try {
    const status = await apiGet(API.BRIDGE.STATUS);
    set('bridgeStatus', status);
    const el = document.getElementById('bridge-status');
    if (el) {
      el.innerHTML = `
        <span>启用: ${status.enabled ? '是' : '否'}</span>
        <span>能力: ${status.capability || 'unknown'}</span>
        <span>下载服务: ${status.dlserver_ready ? '就绪' : '未就绪'}</span>
        <span>待处理: ${(status.pending_out || 0) + (status.pending_in || 0)}</span>
      `;
    }
  } catch (e) {
    console.error('[bridge] status load failed:', e);
  }
}

async function loadBridgeTasks(direction) {
  try {
    const data = await apiPost(API.BRIDGE.TASKS, { direction });
    set('tasks', data.tasks || []);
    renderBridgeTasks();
  } catch (e) {
    console.error('[bridge] tasks load failed:', e);
  }
}

function renderBridgeTasks() {
  const tbody = document.getElementById('bridge-tbody');
  if (!tbody) return;
  const tasks = getState().tasks || [];
  tbody.innerHTML = '';

  if (tasks.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="6" class="empty-hint">暂无任务</td>`;
    tbody.appendChild(tr);
    return;
  }

  for (const task of tasks) {
    const tr = document.createElement('tr');
    tr.dataset.taskId = task.task_id;
    tr.innerHTML = `
      <td>${escapeHtml(String(task.task_id || '').slice(0, 8))}</td>
      <td>${escapeHtml(String(task.resource_id || '-'))}</td>
      <td>${escapeHtml(task.remote_path || '-')}</td>
      <td><span class="badge ${task.state}">${BRIDGE_STATE_LABELS[task.state] || task.state}</span>
        ${task.detail ? `<span class="detail-hint" title="${escapeHtml(task.detail)}">${getIcon('INFO', 12)}</span>` : ''}</td>
      <td>${formatTimeFull(task.updated_at)}</td>
      <td>
        ${task.state === BRIDGE_STATES.FAILED
          ? `<button class="btn-retry" data-task-id="${task.task_id}">重试</button>` : ''}
        ${task.state === BRIDGE_STATES.PENDING || task.state === BRIDGE_STATES.RUNNING
          ? `<button class="btn-cancel" data-task-id="${task.task_id}">取消</button>` : ''}
      </td>
    `;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll('.btn-retry').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await apiPost(API.BRIDGE.RETRY, { task_id: btn.dataset.taskId });
        toast('重试已提交', 'success');
      } catch (e) { toast('重试失败', 'error'); }
      loadBridgeTasks(getState().currentBridgeDirection || 'out');
    });
  });
  tbody.querySelectorAll('.btn-cancel').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await apiPost(API.BRIDGE.CANCEL, { task_id: btn.dataset.taskId });
        toast('已取消', 'success');
      } catch (e) { toast('取消失败', 'error'); }
      loadBridgeTasks(getState().currentBridgeDirection || 'out');
    });
  });
}

async function openBridgeConfig() {
  try {
    const config = await apiGet(API.BRIDGE.CONFIG_GET);
    const rows = CONFIG_FIELDS.map((f) => ({
      ...f,
      value: String(config[f.name] ?? ''),
    }));
    const res = await showFormModal('桥接配置', rows, { okText: '保存' });
    if (!res) return;
    const body = {};
    for (const f of CONFIG_FIELDS) {
      if (res[f.name] !== undefined) {
        if (f.type === 'select') body[f.name] = res[f.name] === 'true';
        else body[f.name] = res[f.name];
      }
    }
    await apiPost(API.BRIDGE.CONFIG_SAVE, body);
    toast('配置已保存', 'success');
    loadBridgeStatus();
  } catch (e) { toast('加载配置失败', 'error'); }
}