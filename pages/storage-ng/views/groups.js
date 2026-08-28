/**
 * Groups view (T-6) - 群组 tab.
 *
 * Thin shell over components/group-table.js which owns the full group
 * management UI: online accounts only (L-4: offline/switched accounts are
 * hidden by the backend, the list decays with scans), client-side sort /
 * pagination, batch ops, remove/restore, labels and ordering. A row click
 * sets the file context and opens the files tab.
 *
 * @module views/groups
 */

import { initGroupsView as mountGroupTable } from '../components/group-table.js';

/**
 * Initialize the groups view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initGroupsView(container) {
  return mountGroupTable(container);
}