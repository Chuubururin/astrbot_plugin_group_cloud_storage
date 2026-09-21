/**
 * DOM diff - keyed row reconciliation .
 *
 * Lists render through applyKeyedDiff: existing <tr> nodes are reused by
 * row key, changed ones are replaced, stale ones removed, and order is
 * fixed with DOM moves - never a full innerHTML rewrite. All mutations
 * are rAF-batched, and each frame stops before the *measured* DOM write
 * count would pass MAX_ROWS_PER_FRAME (enforced against real writes, not op
 * count; the one batched fragment insert counts as a single write and its row
 * count is reported separately as bulkRows). Statistics feed the E2E probes.
 *
 * @module utils/dom-diff
 */

import { MAX_ROWS_PER_FRAME } from '../constants.js';

/** Cumulative keyed-render statistics (exposed to E2E probes). */
const diffStats = {
  totalRenders: 0,
  lastRewrittenRows: 0,
  maxFrameWrites: 0,
  lastFramesUsed: 1,
  maxBulkRows: 0,
  violations: 0,
};

/** Per-container render generation: a newer applyKeyedDiff supersedes any
 * still-chunked older render on the same container (stale-tail guard). */
const runSeq = new WeakMap();

/** Snapshot of the render statistics . */
export function getDiffStats() {
  return { ...diffStats };
}

/**
 * Value equality for one field. Scalars compare by identity; arrays/objects
 * compare by JSON projection, because JSON.parse produces a fresh reference
 * on every poll - identity-only comparison would mark every nested field
 * (tags/meta/payload) as changed and rewrite the whole list every refresh.
 */
function sameValue(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return false;
  if (typeof a !== 'object' || typeof b !== 'object') return false;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
}

/** Field-level equality across the union of both objects' keys. */
function hasChanged(oldItem, newItem) {
  const keys = new Set([...Object.keys(oldItem), ...Object.keys(newItem)]);
  for (const key of keys) {
    if (!sameValue(oldItem[key], newItem[key])) return true;
  }
  return false;
}

/**
 * Apply a keyed diff to a container.
 *
 * @param {HTMLElement} container - tbody (rows carry dataset.key)
 * @param {Array} newItems - row data of the new listing
 * @param {function} renderFn - (item) => HTMLElement (must set dataset.key)
 * @param {string|function} keyFn - key field name or key extractor
 * @returns {{rewrittenRows: number}} rows created/replaced by this render
 */
export function applyKeyedDiff(container, newItems, renderFn, keyFn = 'id') {
  const keyOf = typeof keyFn === 'function' ? keyFn : (item) => String(item[keyFn]);

  const byKey = new Map();
  for (const el of Array.from(container.children)) {
    if (el.dataset && el.dataset.key != null) byKey.set(el.dataset.key, el);
  }

  // Duplicate keys would silently collapse rows: byKey holds one node per
  // key while finalEls has one slot per want entry, so the second slot
  // re-appends the same node (a move, not an insert) and a row disappears.
  // Disambiguate duplicates with a stable ordinal suffix and warn instead.
  const seen = new Map();
  const wantKeys = newItems.map((item) => {
    const raw = keyOf(item);
    const n = (seen.get(raw) || 0) + 1;
    seen.set(raw, n);
    return n === 1 ? raw : `${raw}\u0000dup${n}`;
  });
  if (wantKeys.length !== seen.size) {
    console.warn('[dom-diff] duplicate row keys disambiguated:',
      [...seen].filter(([, n]) => n > 1).map(([k]) => k).join(', '));
  }
  const wantSet = new Set(wantKeys);

  const toRemove = [];
  for (const [key, el] of byKey) {
    if (!wantSet.has(key)) toRemove.push(el);
  }

  let rewritten = 0;
  const plan = newItems.map((item, i) => {
    const key = wantKeys[i];
    const el = byKey.get(key);
    if (!el) return { item, key, create: true };
    if (el.__data !== item && hasChanged(el.__data || {}, item)) {
      return { item, key, el, replace: true };
    }
    return { item, key, el };
  });

  const currentKeys = Array.from(container.children)
    .map((el) => (el.dataset ? el.dataset.key : undefined));
  const orderChanged = currentKeys.length !== newItems.length ||
    wantKeys.some((key, i) => currentKeys[i] !== key);

  // Build the mutation plan. Every op declares `cost` (the DOM writes it is
  // about to perform) and returns the writes it actually performed, so the
  // frame loop can hold the real write count inside the budget.
  const ops = [];
  for (const el of toRemove) {
    ops.push({ cost: 1, run: () => { if (el.isConnected) el.remove(); return 1; } });
  }

  // finalEls has one slot per plan step in want order: keep-steps fill
  // synchronously, create/replace-steps fill their own slot at frame time.
  // A shared push array would strand created/replaced rows at the tail and
  // break the reorder append (2026-09-07 move-up misorder bug).
  const finalEls = new Array(plan.length);

  for (let i = 0; i < plan.length; i++) {
    const step = plan[i];
    if (step.create) {
      ops.push({ cost: 1, run: (liveKeys) => {
        let el = liveKeys.get(step.key);
        if (el) { finalEls[i] = el; return 0; } // raced render already made it
        el = renderFn(step.item);
        el.dataset.key = step.key;
        el.__data = step.item;
        liveKeys.set(step.key, el);
        rewritten += 1;
        finalEls[i] = el;
        return 1;
      } });
    } else if (step.replace) {
      ops.push({ cost: 2, run: (liveKeys) => {
        const el = step.el;
        if (!el.isConnected) {
          const cur = liveKeys.get(step.key);
          if (cur) { finalEls[i] = cur; return 0; } // superseded by a newer render
        }
        const fresh = renderFn(step.item);
        fresh.dataset.key = step.key;
        fresh.__data = step.item;
        el.replaceWith(fresh);
        liveKeys.set(step.key, fresh);
        finalEls[i] = fresh;
        rewritten += 1;
        return 2;
      } });
    } else {
      finalEls[i] = step.el;
      // __data 必须与 DOM 写入同刻更新：在计划期就改写 __data，一旦本次
      // run 被更新的渲染取代（下面的 runSeq 守卫）就会留下「DOM 是旧值、
      // __data 已是新值」的行，后续 hasChanged 判为无变化而长期停留旧数据。
      ops.push({ cost: 0, run: () => { step.el.__data = step.item; return 0; } });
    }
  }

  if (orderChanged || toRemove.length > 0 || plan.some((p) => p.create)) {
    // One batched fragment insert is a single DOM write, so it is charged 1
    // to the frame budget. Charging the row count made every append/reorder
    // of a list longer than one frame exceed the budget on its own, so
    // `violations` warned on normal renders; the real row burst stays visible
    // as the bulkRows metric instead. The op is always last, so the frame
    // pacing and the runSeq supersede semantics are unchanged.
    const moved = finalEls.length;
    ops.push({ cost: 1, bulk: moved, run: () => {
      const frag = document.createDocumentFragment();
      for (const el of finalEls) frag.appendChild(el);
      container.appendChild(frag);
      return 1;
    } });
  }

  const state = runSeq.get(container) || { seq: 0 };
  const myRun = ++state.seq;
  runSeq.set(container, state);

  requestAnimationFrame(() => {
    let cursor = 0;
    let frames = 0;
    // Live key set at frame time: concurrent renders of the same list are
    // idempotent (the second reuses nodes instead of duplicating them).
    const liveKeys = new Map();
    for (const el of Array.from(container.children)) {
      if (el.dataset && el.dataset.key != null) liveKeys.set(el.dataset.key, el);
    }
    const runChunk = () => {
      if (runSeq.get(container)?.seq !== myRun) return; // superseded
      let writes = 0;
      let ran = 0;
      // Budget-aware chunking against the measured write count.
      while (cursor < ops.length && writes + ops[cursor].cost <= MAX_ROWS_PER_FRAME) {
        const op = ops[cursor];
        writes += op.run(liveKeys);
        if (op.bulk) diffStats.maxBulkRows = Math.max(diffStats.maxBulkRows, op.bulk);
        cursor += 1;
        ran += 1;
      }
      if (ran === 0 && cursor < ops.length) {
        // A single op alone costs more than a whole frame budget: it still
        // has to run or the list never settles, and that overrun is exactly
        // what the budget gate exists to report.
        const op = ops[cursor];
        writes += op.run(liveKeys);
        if (op.bulk) diffStats.maxBulkRows = Math.max(diffStats.maxBulkRows, op.bulk);
        cursor += 1;
      }
      frames += 1;
      diffStats.maxFrameWrites = Math.max(diffStats.maxFrameWrites, writes);
      if (writes > MAX_ROWS_PER_FRAME) {
        diffStats.violations += 1;
        console.warn(`[dom-diff] frame wrote ${writes} units (> ${MAX_ROWS_PER_FRAME})`);
      }
      if (cursor < ops.length) {
        requestAnimationFrame(runChunk);
      } else {
        diffStats.totalRenders += 1;
        diffStats.lastRewrittenRows = rewritten;
        diffStats.lastFramesUsed = Math.max(1, frames);
      }
    };
    runChunk();
  });

  return { rewrittenRows: rewritten };
}
