/**
 * Group-context resolution - the single source of the row-first rule.
 *
 * Listing views may omit the group filter (the backend then serves the
 * managed default), so a row's own group_id is authoritative; the
 * module-scoped group state is only the fallback for rows that carry
 * none. Centralizing the resolution order keeps every call site on the
 * same rule and makes regressions of the rule grep-visible.
 *
 * @module utils/group
 */

/**
 * Module-scoped group of a source tab.
 * @param {Object} state - store state
 * @param {string} sourceId - 'group'|'netdisk'|'album'|'essence' (or '')
 * @returns {string} group id ('' when the tab has no group focus)
 */
export function groupFor(state, sourceId) {
  if (sourceId === 'album') return state.albumGroup || '';
  if (sourceId === 'essence') return state.essenceGroup || '';
  return state.currentGroup || '';
}

/**
 * Row-first group resolution: the row's group_id wins, then the
 * module-scoped group of the source tab.
 * @param {Object} state
 * @param {Object} [row]
 * @param {string} sourceId
 * @returns {string}
 */
export function rowGroupFor(state, row, sourceId) {
  return (row && row.group_id) || groupFor(state, sourceId);
}

/**
 * Row-first resolution for the group-files domain (currentGroup
 * fallback). Historical name kept for the command layer; new call
 * sites should prefer rowGroupFor with an explicit sourceId.
 * @param {Object} state
 * @param {Object} [row]
 * @returns {string}
 */
export function rowGroup(state, row) {
  return rowGroupFor(state, row, 'group');
}
