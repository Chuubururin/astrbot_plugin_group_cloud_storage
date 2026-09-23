/**
 * Refresh coalescer - collapses repeat reload triggers into fewer requests.
 *
 * One reload of a list/ledger can be triggered by many sources at once (SSE
 * events of a finishing batch, a click's own post-action reload, filter
 * changes). N triggers are not N needed requests, so both shapes here borrow
 * the mechanism components/data-table.js already uses for the four list
 * sources: absorb the repeats instead of forwarding them.
 *
 * - createRefreshWindow: per-topic time window for `refresh:<topic>` publishers.
 *   Trailing (default) for data_changed bursts; leading for queue-state echoes,
 *   where the first repaint must be immediate (see the option note).
 * - createCoalescedLoader: in-flight guard + dirty tail rerun around one async
 *   loader, so triggers can never overlap into concurrent identical requests.
 *
 * @module utils/refresh-coalescer
 */

import { refresh as publishTopicRefresh } from '../store.js';

/** Repeat-refresh window (ms): matches the data_changed batch cadence. */
export const COALESCE_WINDOW_MS = 150;

/**
 * Per-topic refresh window.
 *
 * trailing (default): every request pushes the timer back, so a stream of
 * data_changed events (one per group/file of a batch) yields one refresh after
 * the burst settles.
 *
 * leading ({leading:true}): the first request refreshes *immediately* and only
 * the repeats inside the window are folded into a single follow-up. Needed for
 * PAUSED/RESUMED/CANCELLED/RETRY: those events are the echo of a click the user
 * just made, so a trailing-only window would keep the pre-click row on screen
 * for the whole window. Collapsing is never "wait and show stale state".
 *
 * @param {Object} [options]
 * @param {boolean} [options.leading=false] - fire the first refresh at once
 * @param {number} [options.delayMs] - window length
 * @param {function} [options.publish] - (topic) => void, defaults to store.refresh
 * @returns {function((string|string[]))} schedule - request one or more topics
 */
export function createRefreshWindow(options = {}) {
  const {
    leading = false, delayMs = COALESCE_WINDOW_MS, publish = publishTopicRefresh,
  } = options;
  const windows = new Map();
  const folded = new Set();

  return function schedule(topics) {
    for (const topic of typeof topics === 'string' ? [topics] : topics) {
      if (!leading) {
        clearTimeout(windows.get(topic));
        windows.set(topic, setTimeout(() => {
          windows.delete(topic);
          publish(topic);
        }, delayMs));
        continue;
      }
      if (windows.has(topic)) { folded.add(topic); continue; }
      publish(topic);
      windows.set(topic, setTimeout(() => {
        windows.delete(topic);
        if (folded.delete(topic)) schedule([topic]);
      }, delayMs));
    }
  };
}

/**
 * Serialize one async loader: the call that arrives while a request is in
 * flight starts nothing, it only marks the loader dirty, and exactly one rerun
 * follows the in-flight one. The rerun calls `run()` afresh, so whatever state
 * the run reads at call time (filters, page, query) is the *current* state -
 * capturing parameters at trigger time is the classic coalescing bug.
 *
 * @param {function} run - the actual request (may reject; never escapes)
 * @returns {{load: function, stats: function}} load() is fire-and-forget
 */
export function createCoalescedLoader(run) {
  let inFlight = false;
  let dirty = false;
  let started = 0;

  async function load() {
    if (inFlight) { dirty = true; return; }
    inFlight = true;
    started += 1;
    try {
      await run();
    } catch (e) {
      console.error('[refresh-coalescer] load failed:', e);
    } finally {
      inFlight = false;
      if (dirty) {
        dirty = false;
        await load();
      }
    }
  }

  /** Observable coalescing state (asserted by the regression tests). */
  return { load, stats: () => ({ started, inFlight, dirty }) };
}
