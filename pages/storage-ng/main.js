/**
 * Main entry - shell assembly and initialization (FE-10).
 *
 * main.js wires the shell: theme following (N-08: host context first,
 * system preference fallback), router with eight lazy views, SSE pipeline
 * with heartbeat watchdog, global error handling and keyboard shortcuts.
 * Views are lazy-loaded via dynamic import; this module stays a shell.
 *
 * @module main
 */

import { initRouter, registerView } from './router.js';
import { getState, set, refresh, pushTaskLog } from './store.js';
import { getContext } from './api.js';
import { initHeader } from './components/header.js';
import { initTabs } from './components/tabs.js';
import { initStatusBar } from './components/status-bar.js';
import { initTaskPanel } from './components/task-panel.js';
import { initStatBar } from './components/stat-bar.js';
import { isE2EMode, initE2EHooks, disableBridge } from './testing/e2e-hooks.js';
import { EVENT_TYPES, DATA_CHANGED_TOPICS, EVENT_KINDS } from './constants.js';
import { createResilientSSE } from './utils/sse.js';
import { toast } from './components/toast.js';

// ---------- Theme (N-08: follow the host AstrBot theme; fall back to the
// system preference and react to live changes) ----------

function initTheme() {
  const ctx = getContext();
  const hostTheme = ctx && (ctx.theme || '').toLowerCase();

  const apply = () => {
    let theme = 'dark';
    if (hostTheme === 'light' || hostTheme === 'dark') {
      theme = hostTheme;
    } else {
      theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light' : 'dark';
    }
    document.documentElement.setAttribute('data-theme', theme);
  };

  apply();
  try {
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
        if (hostTheme !== 'light' && hostTheme !== 'dark') apply();
      });
    }
  } catch (e) { /* matchMedia unsupported */ }
}

// ---------- SSE (FE-14 precise updates, I5 self-healing) ----------

/** Event types that feed the floating task-panel log. */
const TASK_LOG_TYPES = new Set([
  EVENT_TYPES.QUEUED, EVENT_TYPES.STARTED, EVENT_TYPES.PROGRESS,
  EVENT_TYPES.DONE, EVENT_TYPES.FAILED, EVENT_TYPES.RETRY,
]);

/** After a reconnection: one refresh per data topic (FE-4). */
function refreshAllTopics() {
  refresh('groups');
  refresh('files');
  refresh('bridge');
  refresh('tasks');
  if (getState().currentView === 'netdisk') refresh('netdisk');
  refresh('albums');
  refresh('essence');
}

// ---------- 2026-09-03 性能修复（P-3）：data_changed 主题刷新合并 ----------
// 批量任务完成会连续推送 data_changed（file_scan 按群、多任务批量 DONE）：
// 同一主题在 150ms 窗口内合并为一次刷新，避免多主题并发全量拉取的请求风暴。
const _pendingDataRefresh = new Map();

function debouncedTopicRefresh(topics) {
  for (const topic of topics) {
    clearTimeout(_pendingDataRefresh.get(topic));
    _pendingDataRefresh.set(topic, setTimeout(() => {
      _pendingDataRefresh.delete(topic);
      refresh(topic);
    }, 150));
  }
}

function handleSSEEvent(ev) {
  const { type, task_id, kind, state: taskState, percent, detail } = ev;

  if (TASK_LOG_TYPES.has(type)) {
    set('activeTask', type === EVENT_TYPES.DONE || type === EVENT_TYPES.FAILED
      ? null
      : { kind, task_id, i: percent || 0, n: 100, detail: detail || type });
    pushTaskLog(ev);
  }

  switch (type) {
    case EVENT_TYPES.DONE:
      if (kind === EVENT_KINDS.BRIDGE_OUT || kind === EVENT_KINDS.BRIDGE_IN) {
        toast(`${kind === EVENT_KINDS.BRIDGE_OUT ? '转存网盘' : '转存群'}完成`, 'success');
        refresh('bridge');
      }
      break;

    case EVENT_TYPES.FAILED:
      toast(`${kind || '任务'}失败: ${detail || ''}`, 'error');
      break;

    case EVENT_TYPES.DATA_CHANGED: {
      // Only data_changed may reload a topic (FE-14); topics via map.
      // P-3: 150ms 窗口合并（批量事件防风暴）。
      const topics = DATA_CHANGED_TOPICS[kind] || ['files'];
      debouncedTopicRefresh(topics);
      break;
    }

    default:
      break;
  }
}

function initSSE() {
  // Mock E2E mode skips SSE entirely; real-machine mode (auth provided)
  // keeps the live stream.
  if (isE2EMode() && !window.__E2E_AUTH__) return;

  const sse = createResilientSSE({
    onEvent: handleSSEEvent,
    onConnectionChange: (connected) => set('sseConnected', connected),
    onReconnected: refreshAllTopics,
  });
  sse.start();
  window.addEventListener('unload', () => sse.stop());
}

// ---------- Global error handling (A6, I4) ----------

function initErrorHandling() {
  window.addEventListener('error', (e) => {
    console.error('[global] uncaught error:', e.error);
    set('error', e.message || '未知错误');
    toast('页面错误: ' + (e.message || '未知'), 'error');
  });
  window.addEventListener('unhandledrejection', (e) => {
    console.error('[global] unhandled rejection:', e.reason);
    set('error', String(e.reason || '未知错误'));
  });
}

// ---------- Keyboard shortcuts (mainstream control paradigm) ----------

function initKeyboard() {
  document.addEventListener('keydown', async (e) => {
    // Escape: clear the selection of the active resource list.
    if (e.key === 'Escape') {
      const { selectionFor } = await import('./store.js');
      const { sourceFor } = await import('./features/data-sources.js');
      sourceFor(getState().currentView).selection.clear();
      return;
    }
    // Ctrl/Cmd+A: select all rows of the active list (not inside inputs).
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      const tag = e.target?.tagName;
      if (tag && /INPUT|TEXTAREA|SELECT/.test(tag)) return;
      e.preventDefault();
      const { sourceFor } = await import('./features/data-sources.js');
      const source = sourceFor(getState().currentView);
      const items = getState()[source.itemsKey] || [];
      source.selection.setMany(items.filter((f) => !f.is_dir).map(source.rowKey));
    }
  });
}

// ---------- Init ----------

function init() {
  if (isE2EMode()) {
    console.log('[main] E2E mode detected');
    disableBridge();
    initE2EHooks();
  }

  initTheme();
  initErrorHandling();
  initKeyboard();

  initHeader(document.getElementById('header'));
  initTabs(document.getElementById('tabs'));
  initStatusBar(document.getElementById('status-bar'));
  initTaskPanel();
  initStatBar(document.getElementById('stat-bar'));

  // ---- View registration (lazy loaded, FE-10) ----
  // v2.13: import 完成后检查容器是否仍在 DOM，防止快速切 tab 时旧 import
  // 把 DOM 写进已被新视图占用的 #content（router generation 仅保护 cleanup，
  // 不阻止 render 内部的 DOM 挂载）。
  registerView('files', async (container) => {
    const { initFilesView } = await import('./views/files.js');
    if (!container.isConnected) return () => {};
    return initFilesView(container);
  });
  registerView('albums', async (container) => {
    const { initAlbumsView } = await import('./views/albums.js');
    if (!container.isConnected) return () => {};
    return initAlbumsView(container);
  });
  registerView('essence', async (container) => {
    const { initEssenceView } = await import('./views/essence.js');
    if (!container.isConnected) return () => {};
    return initEssenceView(container);
  });
  registerView('netdisk', async (container) => {
    const { initNetdiskView } = await import('./views/netdisk.js');
    if (!container.isConnected) return () => {};
    return initNetdiskView(container);
  });
  registerView('tasks', async (container) => {
    const { initTasksView } = await import('./views/tasks.js');
    if (!container.isConnected) return () => {};
    return initTasksView(container);
  });
  registerView('groups', async (container) => {
    const { initGroupsView } = await import('./views/groups.js');
    if (!container.isConnected) return () => {};
    return initGroupsView(container);
  });
  registerView('config', async (container) => {
    const { initConfigView } = await import('./views/config.js');
    if (!container.isConnected) return () => {};
    return initConfigView(container);
  });

  // CT-9 classification table preload (drives netdisk local chips).
  import('./api.js').then(async ({ apiGet, API }) => {
    try { set('extTypes', await apiGet(API.META_CLASSIFY)); }
    catch (e) { console.warn('[main] classify table unavailable:', e); }
  });

  initRouter();
  initSSE();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}