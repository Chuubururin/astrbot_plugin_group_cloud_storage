/**
 * DOM diff - keyed row reconciliation .
 *
 * Lists render through applyKeyedDiff: existing <tr> nodes are reused by row
 * key, changed ones are replaced, stale ones removed, and order is fixed with
 * the *minimal* set of DOM moves (longest increasing subsequence) - never a
 * full innerHTML rewrite, and never a full-list re-append. All mutations are
 * rAF-batched, and each frame stops before the *measured* frame units would
 * pass MAX_ROWS_PER_FRAME (one unit rebuilds one row; a reparent move is
 * charged a fraction - see MOVE_UNITS). Those units are a scheduling weight,
 * not a DOM-op count: diff-stats reports real mutations separately.
 *
 * Change detection is a per-row render signature when the caller supplies
 * `signatureFn`, and a whole-DTO field compare otherwise (warned once).
 * @module utils/dom-diff
 */

import { MAX_ROWS_PER_FRAME } from '../constants.js';
import { stableRun } from './lis.js';
import { hasChanged } from './dto-equal.js';
import { noteMutation, stats as diffStats } from './diff-stats.js';

/** Per-container render generation: a newer applyKeyedDiff supersedes any
 * still-chunked older render on the same container (stale-tail guard). */
const runSeq = new WeakMap();

export { getStats as getDiffStats } from './diff-stats.js';

/**
 * A move is a reparent (pointer surgery), not content construction, so it is
 * charged a fraction of a row rebuild against the frame budget: a whole-page
 * reorder settles in one frame instead of two, while a pathological reorder of
 * hundreds of rows still chunks.
 */
const MOVE_UNITS = 0.25;

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
  // One render generation per container, shared with the supersede guard below.
  const state = runSeq.get(container) || { seq: 0, sigWarned: false };
  // Every list caller is expected to project what it renders: the fallback
  // compares the whole DTO, so unrelated server-side fields rebuild rows on
  // every poll. Warn once per container so the omission names itself.
  if (!signatureFn && !state.sigWarned) {
    state.sigWarned = true;
    console.warn('[dom-diff] caller omitted signatureFn on',
      container.id || container.className || container.tagName,
      '- falling back to the whole-DTO compare');
  }
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
    ops.push({ cost: 1, run: () => { if (el.isConnected) { el.remove(); noteMutation(); } return 1; } });
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
        // replaceWith() is a silent no-op on a detached node, so a row another
        // writer already removed has to be appended instead.
        if (el.parentNode === container) el.replaceWith(fresh);
        else container.appendChild(fresh);
        noteMutation();
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
  // run move, and each move is charged MOVE_UNITS against the frame budget, so
  // a reorder of a whole page still fits one frame while a pathological one
  // chunks rather than overrunning.
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
      ops.push({ cost: MOVE_UNITS, run: () => {
        const anchor = refIdx === null ? null : finalEls[refIdx];
        // Other code writes these containers directly (empty state, pane
        // switches, error rows), so the anchor may be detached by the time this
        // frame runs. insertBefore would throw NotFoundError and abort every
        // remaining op, leaving the list half-updated; appending to the tail
        // keeps the row reachable and the next render re-plans the order.
        if (anchor && anchor.parentNode !== container) container.appendChild(finalEls[target]);
        else container.insertBefore(finalEls[target], anchor);
        noteMutation();
        return MOVE_UNITS;
      } });
      plannedMoves += 1;
      ref = target;
    }
  }

  const myRun = ++state.seq;
  runSeq.set(container, state);

  const runMutations = diffStats.mutations;
  requestAnimationFrame(() => {
    let cursor = 0;
    let frames = 0;
    const runChunk = () => {
      if (runSeq.get(container)?.seq !== myRun) return; // superseded
      // Live key set, rebuilt per frame: other code writes this container
      // between frames, and a map captured once at run start would hand the
      // create/replace dedupe a detached node.
      const liveKeys = new Map();
      for (const el of Array.from(container.children)) {
        if (el.dataset && el.dataset.key != null) liveKeys.set(el.dataset.key, el);
      }
      let writes = 0;
      let ran = 0;
      const frameMutations = diffStats.mutations;
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
      const frameDone = diffStats.mutations - frameMutations;
      diffStats.maxFrameWrites = Math.max(diffStats.maxFrameWrites, writes);
      diffStats.maxFrameMutations = Math.max(diffStats.maxFrameMutations, frameDone);
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
        diffStats.lastMutations = diffStats.mutations - runMutations;
      }
    };
    runChunk();
  });

  return { rewrittenRows: rewritten, moves: plannedMoves };
}
