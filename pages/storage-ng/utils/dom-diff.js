/**
 * DOM diff - keyed row reconciliation (FE-12/13).
 *
 * Lists render through applyKeyedDiff: existing <tr> nodes are reused by
 * row key, changed ones are replaced, stale ones removed, and order is
 * fixed with DOM moves - never a full innerHTML rewrite. All mutations
 * are rAF-batched in chunks capped by MAX_ROWS_PER_FRAME so no single
 * animation frame exceeds the render budget (FE-16); statistics are
 * exposed for the E2E acceptance probes (TE-1).
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
  violations: 0,
};

/** Snapshot of the render statistics (FE-16 acceptance data). */
export function getDiffStats() {
  return { ...diffStats };
}

/** Shallow equality across the union of both objects' keys. */
function hasChanged(oldItem, newItem) {
  const keys = new Set([...Object.keys(oldItem), ...Object.keys(newItem)]);
  for (const key of keys) {
    if (oldItem[key] !== newItem[key]) return true;
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

  const wantKeys = newItems.map(keyOf);
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

  // Build the mutation plan; writes are chunked per frame later.
  const ops = [];
  for (const el of toRemove) {
    ops.push(() => { if (el.isConnected) el.remove(); });
  }

  const finalEls = [];
  for (const step of plan) {
    if (step.create) {
      ops.push((liveKeys) => {
        let el = liveKeys.get(step.key);
        if (el) { finalEls.push(el); return; }        // raced render already made it
        el = renderFn(step.item);
        el.dataset.key = step.key;
        el.__data = step.item;
        liveKeys.set(step.key, el);
        finalEls.push(el);
        rewritten += 1;
      });
    } else if (step.replace) {
      ops.push((liveKeys) => {
        let el = step.el;
        if (!el.isConnected) {
          const cur = liveKeys.get(step.key);
          if (cur) { finalEls.push(cur); return; }     // superseded by a newer render
        }
        const fresh = renderFn(step.item);
        fresh.dataset.key = step.key;
        el.replaceWith(fresh);
        liveKeys.set(step.key, fresh);
        finalEls.push(fresh);
        rewritten += 1;
      });
    } else {
      finalEls.push(step.el);
      step.el.__data = step.item;
    }
  }

  if (orderChanged || toRemove.length > 0 || plan.some((p) => p.create)) {
    ops.push(() => {
      const frag = document.createDocumentFragment();
      for (const el of finalEls) frag.appendChild(el);
      container.appendChild(frag);
    });
  }

  requestAnimationFrame(() => {
    let frame = 0;
    // Live key set at frame time: concurrent renders of the same list are
    // idempotent (the second reuses nodes instead of duplicating them).
    const liveKeys = new Map();
    for (const el of Array.from(container.children)) {
      if (el.dataset && el.dataset.key != null) liveKeys.set(el.dataset.key, el);
    }
    const runChunk = () => {
      const chunk = ops.slice(frame * MAX_ROWS_PER_FRAME, (frame + 1) * MAX_ROWS_PER_FRAME);
      for (const op of chunk) op(liveKeys);
      frame += 1;
      const writes = Math.max(chunk.length, 1);
      diffStats.maxFrameWrites = Math.max(diffStats.maxFrameWrites, writes);
      if (writes > MAX_ROWS_PER_FRAME) {
        diffStats.violations += 1;
        console.warn(`[dom-diff] frame wrote ${writes} units (> ${MAX_ROWS_PER_FRAME})`);
      }
      if (frame * MAX_ROWS_PER_FRAME < ops.length) {
        requestAnimationFrame(runChunk);
      } else {
        diffStats.totalRenders += 1;
        diffStats.lastRewrittenRows = rewritten;
        diffStats.lastFramesUsed = Math.max(1, Math.ceil(ops.length / MAX_ROWS_PER_FRAME));
      }
    };
    runChunk();
  });

  return { rewrittenRows: rewritten };
}