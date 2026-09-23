/**
 * DOM diff - keyed row reconciliation .
 *
 * Lists render through applyKeyedDiff: existing <tr> nodes are reused by row
 * key, changed ones are replaced, stale ones removed, and order is fixed with
 * the *minimal* set of DOM moves (longest increasing subsequence) - never a
 * full innerHTML rewrite, and never a full-list re-append. All mutations are
 * rAF-batched, and each frame stops before the *measured* DOM write count
 * would pass MAX_ROWS_PER_FRAME (enforced against real writes, not op count).
 * Statistics feed the E2E probes.
 *
 * Change detection is a per-row render signature when the caller supplies
 * `signatureFn`, and a whole-DTO field compare otherwise.
 * @module utils/dom-diff
 */

import { MAX_ROWS_PER_FRAME } from '../constants.js';

/** Cumulative keyed-render statistics (exposed to E2E probes). */
const diffStats = {
  totalRenders: 0,
  lastRewrittenRows: 0,
  maxFrameWrites: 0,
  lastFramesUsed: 1,
  lastMoves: 0,
  maxMoves: 0,
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
 * Value equality for one field: scalars by identity, arrays/objects by JSON
 * projection (JSON.parse yields a fresh reference per poll, so identity alone
 * would mark every nested field changed). This is the *fallback* comparator -
 * it compares the whole DTO, so fields the row never renders still force a
 * replacement. Callers that know what they render pass `signatureFn`.
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
 * Longest increasing subsequence over the `c` (current DOM index) of a
 * want-order scan; returns indices into `seq` of one optimal stable run.
 * Rows in it already sit in final relative order, so N - LIS is the provable
 * minimum number of DOM moves. Patience sorting: O(N log N).
 */
function stableRun(seq) {
  const tails = [];
  const prev = new Array(seq.length).fill(-1);
  for (let i = 0; i < seq.length; i++) {
    const c = seq[i].c;
    let lo = 0;
    let hi = tails.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (seq[tails[mid]].c < c) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0) prev[i] = tails[lo - 1];
    tails[lo] = i;
  }
  const run = [];
  if (!tails.length) return run;
  for (let k = tails[tails.length - 1]; k !== -1; k = prev[k]) run.push(k);
  return run;
}

/**
 * Apply a keyed diff to a container.
 *
 * @param {HTMLElement} container - tbody (rows carry dataset.key)
 * @param {Array} newItems - row data of the new listing
 * @param {function} renderFn - (item) => HTMLElement (must set dataset.key)
 * @param {string|function} keyFn - key field name or key extractor
 * @param {function} [signatureFn] - (item) => string projection of the fields
 *   renderFn reads; replaces the whole-DTO compare with a string identity check
 * @returns {{rewrittenRows: number, moves: number}} rows rewritten / moves planned
 */
export function applyKeyedDiff(container, newItems, renderFn, keyFn = 'id', signatureFn = null) {
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

  // Change detection: a render-signature identity check when the caller
  // declares what it renders, otherwise the whole-DTO field compare.
  const sigOf = signatureFn ? (item) => String(signatureFn(item)) : null;

  let rewritten = 0;
  const plan = newItems.map((item, i) => {
    const key = wantKeys[i];
    const el = byKey.get(key);
    if (!el) return { item, key, create: true, sig: sigOf ? sigOf(item) : undefined };
    if (el.__data !== item) {
      const sig = sigOf ? sigOf(item) : undefined;
      const dirty = sigOf ? el.__sig !== sig : hasChanged(el.__data || {}, item);
      if (dirty) return { item, key, el, replace: true, sig };
      return { item, key, el, sig };
    }
    return { item, key, el, sig: sigOf ? el.__sig : undefined };
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
        el.__sig = step.sig;
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
        fresh.__sig = step.sig;
        el.replaceWith(fresh);
        liveKeys.set(step.key, fresh);
        finalEls[i] = fresh;
        rewritten += 1;
        return 2;
      } });
    } else {
      finalEls[i] = step.el;
      // __data/__sig 必须与 DOM 写入同刻更新：在计划期就改写，一旦本次
      // run 被更新的渲染取代（下面的 runSeq 守卫）就会留下「DOM 是旧值、
      // 签名已是新值」的行，后续比较判为无变化而长期停留旧数据。
      ops.push({ cost: 0, run: () => {
        step.el.__data = step.item;
        if (sigOf) step.el.__sig = step.sig;
        return 0;
      } });
    }
  }

  // ---- reorder: minimal move set ------------------------------------------
  // The previous pass re-appended every row, so moving one row near the top of
  // a 100-row page cost 100 DOM moves. Here only the rows outside the stable
  // run move, and each move is charged 1 write, so even a pathological reorder
  // is honestly chunked across frames rather than overrunning one.
  let plannedMoves = 0;
  if (orderChanged || toRemove.length > 0 || plan.some((p) => p.create)) {
    const curIdx = new Map();
    currentKeys.forEach((key, i) => { if (key != null) curIdx.set(key, i); });
    // Scan in want order, carrying each present row's current DOM index.
    const seq = [];
    for (let i = 0; i < wantKeys.length; i++) {
      const c = curIdx.get(wantKeys[i]);
      if (c !== undefined) seq.push({ w: i, c });
    }
    const stable = new Set(stableRun(seq).map((k) => seq[k].w));
    // Right-to-left: `ref` is the row that must immediately follow the one
    // being placed, and it is already settled - either it was stable, or the
    // previous op just moved it. That is what makes the plan safe to chunk.
    let ref = null;
    for (let i = finalEls.length - 1; i >= 0; i--) {
      if (stable.has(i)) { ref = i; continue; }
      const target = i;
      const refIdx = ref;
      ops.push({ cost: 1, run: () => {
        container.insertBefore(
          finalEls[target], refIdx === null ? null : finalEls[refIdx]);
        return 1;
      } });
      plannedMoves += 1;
      ref = target;
    }
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
        cursor += 1;
        ran += 1;
      }
      if (ran === 0 && cursor < ops.length) {
        // A single op alone costs more than a whole frame budget: it still
        // has to run or the list never settles, and that overrun is exactly
        // what the budget gate exists to report.
        writes += ops[cursor].run(liveKeys);
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
        // Recorded only for a run that actually settled: a superseded render
        // never performed its planned moves and must not report them.
        diffStats.lastMoves = plannedMoves;
        diffStats.maxMoves = Math.max(diffStats.maxMoves, plannedMoves);
      }
    };
    runChunk();
  });

  return { rewrittenRows: rewritten, moves: plannedMoves };
}
