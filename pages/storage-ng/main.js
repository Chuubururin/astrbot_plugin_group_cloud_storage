/**
 * Main entry - shell assembly and initialization .
 *
 * main.js wires the shell: theme following (host context first,
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
import { EVENT_TYPES, DATA_CHANGED_TOPICS, EVENT_KINDS } from './constants.js';
import { createResilientSSE } from './utils/sse.js';
import { toast } from './components/toast.js';

// ---------- Theme (follow the host AstrBot theme; fall back to the
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

// ---------- SSE  ----------

/** Event types that feed the floating task-panel log. */
const TASK_LOG_TYPES = new Set([
  EVENT_TYPES.QUEUED, EVENT_TYPES.STARTED, EVENT_TYPES.PROGRESS,
  EVENT_TYPES.DONE, EVENT_TYPES.FAILED, EVENT_TYPES.RETRY,
]);

/** After a reconnection: one refresh per data topic. */
function refreshAllTopics() {
  lastDataRefreshAt = Date.now();
  refresh('groups');
  refresh('files');
  refresh('bridge');
  refresh('tasks');
  if (getState().currentView === 'netdisk') refresh('netdisk');
  refresh('albums');
  refresh('essence');
}

// ---------- data_changed topic refresh coalescing ----------
// Batch task completion pushes consecutive data_changed events (file_scan
// per group, batch DONE across multiple tasks): refreshes for the same
// topic within a 150ms window coalesce into one refresh, avoiding a
// request storm of concurrent full refetches across topics.
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
      // A backend task failure may still have mutated cloud state
      // (partial uploads, half-applied batches): hot-reload the affected
      // topics so the visible rows match the cloud instead of going stale.
      toast(`${kind || '任务'}失败: ${detail || ''}`, 'error');
      debouncedTopicRefresh(DATA_CHANGED_TOPICS[kind] || ['files']);
      break;

    case EVENT_TYPES.DATA_CHANGED: {
      // Only data_changed may reload a topic ; topics via map.
      // 150ms window coalescing (storm guard for batch events).
      const topics = DATA_CHANGED_TOPICS[kind] || ['files'];
      lastDataRefreshAt = Date.now();
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

  // Visibility staleness guard: after a long time in a hidden tab, missed
  // SSE events (or silent connection decay) can leave stale rows. When the
  // tab becomes visible again and the last data refresh is older than
  // ~60s, do an internal refresh of every data topic.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - lastDataRefreshAt < 60_000) return;
    lastDataRefreshAt = Date.now();
    refreshAllTopics();
  });
}

/** Timestamp of the last data refresh (SSE-driven or reconnect). */
let lastDataRefreshAt = Date.now();

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

/** E2E mode detection inline (testing/ is not shipped with the plugin —
 * a GitHub install lacks the file, and any top-level import of it would
 * 404 the whole module graph and blank the page). */
function isE2EMode() {
  return new URLSearchParams(window.location.search).get('e2e') === '1';
}

async function init() {
  if (isE2EMode()) {
    console.log('[main] E2E mode detected');
    const hooks = await import('./testing/e2e-hooks.js');
    hooks.disableBridge();
    hooks.initE2EHooks();
  }

  initTheme();
  initErrorHandling();
  initKeyboard();

  initHeader(document.getElementById('header'));
  initTabs(document.getElementById('tabs'));
  initStatusBar(document.getElementById('status-bar'));
  initTaskPanel();
  initStatBar(document.getElementById('stat-bar'));

  // ---- View registration  ----
  // After the import resolves, check the container is still connected:
  // on a fast tab switch a stale import must not write its DOM into
  // #content already owned by the new view (the router generation counter
  // only guards cleanup, not DOM mounts inside render).
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

  // classification table preload (drives netdisk local chips).
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