/**
 * Group list ordering - the client-side comparator of the groups model.
 *
 * The groups endpoint returns the full list (no server pagination), so
 * sorting is a pure local concern. Extracted from features/group-data.js
 * so that module stays within the <=300-line budget.
 *
 * @module features/group-sort
 */

/**
 * Sort field value of one group. `last_scan` unifies the two shapes the
 * backend may send (`last_scan_at` / `last_scan`) using the same fallback
 * pair the row renderer uses, so the header sort key and the rendered
 * value never disagree.
 * @param {Object} g
 * @param {string} key
 * @returns {number|string}
 */
function sortValue(g, key) {
  if (key === 'last_scan') return g.last_scan_at || g.last_scan || '';
  return g[key] ?? '';
}

/**
 * Sort a group list by the store's sort descriptor.
 *
 * `group_id` / `label` / `last_scan` carry numeric semantics: comparing
 * them as strings puts "10001" before "9999". Numeric-looking values use a
 * numeric compare; everything else (names, ISO timestamps, non-numeric
 * labels) falls back to a locale string compare.
 * @param {Object[]} groups
 * @param {{key: string, dir: string}} sort
 * @returns {Object[]} new sorted array (input untouched)
 */
export function sortGroups(groups, sort) {
  const { key, dir } = sort;
  const mul = dir === 'asc' ? 1 : -1;
  return [...groups].sort((a, b) => {
    if (key === 'used_space') return ((a.used_space || 0) - (b.used_space || 0)) * mul;
    if (key === 'sort_order') return ((a.sort_order ?? 1e9) - (b.sort_order ?? 1e9)) * mul;
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    const na = Number(va);
    const nb = Number(vb);
    if (va !== '' && vb !== '' && Number.isFinite(na) && Number.isFinite(nb)) {
      return (na - nb) * mul;
    }
    return String(va || '').localeCompare(String(vb || '')) * mul;
  });
}
