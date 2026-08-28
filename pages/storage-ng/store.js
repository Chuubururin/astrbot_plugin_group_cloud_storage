/**
 * Store - single source of truth for application state (FE-2).
 *
 * All mutations go through set()/update(); components subscribe via
 * subscribe(). Selection sets are module-scoped: each data source owns
 * its own selection via selectionFor() so albums/essence never interfere
 * with the file selection (ADR-0010 W3-A).
 *
 * @module store
 */

import { initialState } from './store-state.js';
import { TASK_LOG_LIMIT } from './constants.js';

const subscribers = new Map();
let state = { ...initialState };

/**
 * Snapshot of the current state (read-only; mutate via set/update).
 * @returns {Object}
 */
export function getState() {
  return state;
}

/** Set one key and notify its subscribers. */
export function set(key, value) {
  state[key] = value;
  notify(key);
}

/** Apply a partial update and notify each changed key. */
export function update(updates) {
  for (const [k, v] of Object.entries(updates)) state[k] = v;
  for (const key of Object.keys(updates)) notify(key);
}

/**
 * Subscribe to state keys.
 * @param {string|string[]} keys - key(s) to watch
 * @param {function} callback - (value, fullState) => void
 * @returns {function} unsubscribe
 */
export function subscribe(keys, callback) {
  const keyList = Array.isArray(keys) ? keys : [keys];
  for (const key of keyList) {
    if (!subscribers.has(key)) subscribers.set(key, new Set());
    subscribers.get(key).add(callback);
  }
  return () => {
    for (const key of keyList) subscribers.get(key)?.delete(callback);
  };
}

/** Subscribe to every state change (used by E2E diagnostics). */
export function subscribeAll(callback) {
  return subscribe('*', callback);
}

function notify(key) {
  if (subscribers.has(key)) {
    for (const cb of subscribers.get(key)) {
      try { cb(state[key], state); } catch (e) { console.error(`[store] subscriber error for ${key}:`, e); }
    }
  }
  if (subscribers.has('*')) {
    for (const cb of subscribers.get('*')) {
      try { cb(state); } catch (e) { console.error('[store] wildcard subscriber error:', e); }
    }
  }
}

// ---- Refresh scheduler (FE-4: one notify per topic, views reload on it) ----

/**
 * Ask every subscriber of a topic to reload. Views subscribe to
 * `refresh:<topic>`; nobody calls load* directly more than once.
 * @param {string} topic - 'files'|'albums'|'essence'|'netdisk'|'groups'|'bridge'|'tasks'|'config'
 */
export function refresh(topic) {
  notify(`refresh:${topic}`);
}

// ---- Generic module selection (W3-A: per-module sets) ----

/**
 * Build a selection helper bound to one store key.
 * @param {string} key - store key holding the Set ('fileSelected', ...)
 */
export function selectionFor(key) {
  return {
    toggle(id, on) {
      const cur = new Set(state[key] || []);
      const sid = String(id);
      if (on) cur.add(sid); else cur.delete(sid);
      set(key, cur);
    },
    setMany(ids) {
      set(key, new Set(ids.map(String)));
    },
    clear() {
      set(key, new Set());
    },
    has(id) {
      return (state[key] || new Set()).has(String(id));
    },
  };
}

/**
 * Bulk file selection with row metadata backfill (select-all/marquee path).
 * @param {string[]} ids - row ids (string form)
 */
export function setFileSelectionMany(ids) {
  const keySet = new Set(ids.map(String));
  const rows = new Map();
  for (const f of state.fileItems) {
    const sid = String(f.id);
    if (keySet.has(sid)) rows.set(sid, f);
  }
  state.fileSelected = keySet;
  state.fileSelRows = rows;
  notify('fileSelected');
}

/** Row metadata map for the currently selected files. */
export function getSelectedFileRows() {
  return state.fileItems.filter((f) => state.fileSelected.has(String(f.id)));
}

// ---- Task log (floating panel behind the queue indicator) ----

/** Append one SSE task event to the log (newest first, capped). */
export function pushTaskLog(ev) {
  const entry = { log_id: `${ev.ts || Date.now()}-${ev.task_id || ''}-${ev.type}`, ...ev };
  state.taskLog = [entry, ...state.taskLog].slice(0, TASK_LOG_LIMIT);
  notify('taskLog');
}

/** Clear the task log. */
export function clearTaskLog() {
  state.taskLog = [];
  notify('taskLog');
}

// ---- Layout preference (N-07 rule 3: single pane default, persisted) ----

export function setLayout(mode) {
  state.layout = mode === 'dual' ? 'dual' : 'single';
  try { localStorage.setItem('cs_layout', state.layout); } catch (e) { /* storage denied */ }
  notify('layout');
}

// ---- Busy state (mainstream: in-flight command indicator) ----

/** Mark a command id as in flight (idempotent). */
export function markBusy(id) {
  if (state.busyKeys.has(id)) return;
  state.busyKeys = new Set(state.busyKeys).add(id);
  notify('busyKeys');
}

/** Clear a command id from the busy set. */
export function unmarkBusy(id) {
  if (!state.busyKeys.has(id)) return;
  const next = new Set(state.busyKeys);
  next.delete(id);
  state.busyKeys = next;
  notify('busyKeys');
}

/** Whether a command is in flight or the app is busy loading. */
export function isBusy(id) {
  return state.busyKeys.has(id) || state.loading;
}

// ---- Stale-response guard (drop out-of-order list responses) ----

/** Allocate the next request sequence for a topic. */
export function nextSeq(topic) {
  const seq = (state.reqSeq[topic] || 0) + 1;
  state.reqSeq = { ...state.reqSeq, [topic]: seq };
  return seq;
}

/** Whether a response (issued at seq) has been superseded. */
export function isStale(topic, seq) {
  return seq < (state.reqSeq[topic] || 0);
}