/**
 * Header - top bar (A1-A6).
 *
 * Title with current group context, home/refresh shortcuts, the queue
 * indicator (opens the task panel) and the layout preference toggle
 * .
 *
 * @module components/header
 */

import { getState, set, subscribe, setLayout, refresh } from '../store.js';
import { navigate } from '../router.js';
import { getIcon } from '../icons.js';
import { API, apiGet } from '../api.js';
import { KIND_LABEL } from '../views/task-labels.js';

/** Neutral hint text; the interval is appended only once it is known. */
const AUTO_SYNC_HINT = '群信息定时同步';
/** Config key behind the hint (core/config/defaults.py). */
const AUTO_SYNC_KEY = 'auto_scan_interval_hours';

/**
 * Initialize the top bar.
 * @param {HTMLElement} container - <header id="header">
 */
export function initHeader(container) {
  container.className = 'topbar';
  container.innerHTML = `
    <div class="header-left">
      <button id="btn-home" class="icon-btn" title="返回群列表">${getIcon('HOME')}</button>
      <h1 class="header-title">群云存储管理</h1>
    </div>
    <div class="header-right">
      <span id="queue-indicator" class="queue-bar" title="队列状态">
        <span class="queue-label">队列</span>
        <b id="queue-count">-</b>
      </span>
      <span class="auto-hint" title="群信息由定时任务自动维护">${AUTO_SYNC_HINT}</span>
      <button id="btn-refresh" class="icon-btn" title="刷新当前视图">${getIcon('REFRESH')}</button>
      <button id="btn-layout" class="layout-toggle-btn" title="列表布局：双栏 / 单栏">单栏</button>
    </div>
  `;

  container.querySelector('#btn-home')?.addEventListener('click', () => navigate('groups'));
  container.querySelector('#btn-refresh')?.addEventListener('click', () => {
    const view = getState().currentView;
    refresh(view);
    // 网盘页的桥接面板只订阅 refresh:bridge（不属于 netdisk 主题）,
    // 不补这一下「刷新当前视图」看着桥接表还是旧数据。
    if (view === 'netdisk') refresh('bridge');
  });
  container.querySelector('#queue-indicator')?.addEventListener('click', () => {
    set('taskPanelOpen', !getState().taskPanelOpen);
  });

  // SSE connection state (I5 self-healing indicator).
  subscribe('sseConnected', (connected) => {
    const el = container.querySelector('#queue-indicator');
    if (el) el.classList.toggle('sse-offline', !connected);
  });

  // Layout preference toggle (persisted; label mirrors the active mode).
  const layoutBtn = container.querySelector('#btn-layout');
  const renderLayout = (mode) => {
    if (layoutBtn) layoutBtn.textContent = mode === 'dual' ? '双栏' : '单栏';
  };
  renderLayout(getState().layout);
  subscribe('layout', renderLayout);
  layoutBtn?.addEventListener('click', () => {
    setLayout(getState().layout === 'dual' ? 'single' : 'dual');
  });

  subscribe('currentGroup', (group) => {
    const title = container.querySelector('.header-title');
    if (title) title.textContent = group ? `群 ${group} - 文件管理` : '群云存储管理';
  });
  subscribe('activeTask', (task) => {
    const el = container.querySelector('#queue-count');
    if (el) el.textContent = task
      ? `${KIND_LABEL[task.kind] || task.kind || '-'} ${task.i}/${task.n}`
      : '-';
  });
  subscribe('queueStatus', (status) => {
    const el = container.querySelector('#queue-count');
    if (el && !getState().activeTask) el.textContent = status?.pending || '-';
  });

  loadAutoSyncHint(container);
}

/**
 * Label the auto-sync hint with the configured interval. The interval is a
 * user setting, so the hint starts neutral and only states a number once
 * config/get answers; a failed or missing read leaves the neutral text.
 */
async function loadAutoSyncHint(container) {
  let hours;
  try {
    const data = await apiGet(API.CONFIG_GET);
    for (const group of data?.groups || []) {
      const item = (group.items || []).find((it) => it.key === AUTO_SYNC_KEY);
      if (item) { hours = Number(item.value); break; }
    }
  } catch (e) {
    return;
  }
  const el = container.querySelector('.auto-hint');
  if (!el || !Number.isFinite(hours)) return;
  // 0 disables the scheduled scan (core/runtime/lifecycle.py), so it must not
  // read as "runs every 0h".
  if (hours > 0) el.textContent = `${AUTO_SYNC_HINT}（${hours}h）`;
  else if (hours === 0) el.textContent = `${AUTO_SYNC_HINT}（已关闭）`;
}
