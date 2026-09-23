/**
 * DOM-diff render statistics - the observability half of utils/dom-diff.
 *
 * The frame budget is charged in weighted scheduling units, so the unit counts
 * and the real DOM-mutation counts are reported separately:
 *
 * - `maxFrameWrites` - weighted scheduling units in the worst frame
 * - `lastMoves` / `maxMoves` - moves the LIS plan asked for
 * - `mutations` / `maxFrameMutations` - real DOM mutations performed
 * - `lastMutations` - real writes the last settled render did
 * - `lastRewrittenRows` - rows created or replaced
 * - `lastFramesUsed` - frames that render spanned
 * - `violations` - frames whose weighted units passed the cap
 *
 * `mutations` counts only calls that touch the container (remove, replaceWith,
 * insertBefore, appendChild). A create op builds a detached row and is not a
 * mutation: that row enters the DOM through a move op, which is counted there.
 *
 * @module utils/diff-stats
 */

/** Cumulative keyed-render statistics (exposed to E2E probes). */
export const stats = {
  totalRenders: 0,
  lastRewrittenRows: 0,
  maxFrameWrites: 0,
  lastFramesUsed: 1,
  lastMoves: 0,
  maxMoves: 0,
  mutations: 0,
  lastMutations: 0,
  maxFrameMutations: 0,
  violations: 0,
};

/** Snapshot of the render statistics. */
export function getStats() {
  return { ...stats };
}

/** Record one real DOM mutation performed against a container. */
export function noteMutation() {
  stats.mutations += 1;
}
