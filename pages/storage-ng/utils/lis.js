/**
 * Longest increasing subsequence for keyed DOM reconciliation.
 *
 * Split out of utils/dom-diff.js, which is at its 300-line module budget;
 * this pass is self-contained and independently testable.
 *
 * @module utils/lis
 */

/**
 * Longest increasing subsequence over the `c` (current DOM index) of a
 * want-order scan; returns indices into `seq` of one optimal stable run.
 * Rows in it already sit in final relative order, so N - LIS is the provable
 * minimum number of DOM moves. Patience sorting: O(N log N).
 */
export function stableRun(seq) {
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
