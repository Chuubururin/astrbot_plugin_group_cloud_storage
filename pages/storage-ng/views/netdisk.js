/**
 * Netdisk view  - OpenList storage tab.
 *
 * Layout:
 *   1. netdisk toolbar (upload local/URL, root, mkdir, type chips)
 *   2. unified data table over the netdisk source (browse, local type
 *      filter/sort, ../ up-level navigation)
 *   3. action bar (link, download, rename, tags, delete, distribute
 *      local/group/album/essence)
 *
 * OpenList connection config lives in the config tab (config center
 * grouping); transfer tasks live in the tasks tab (where the four
 * ledger actions are recorded).
 *
 * @module views/netdisk
 */

import { initDataTable } from '../components/data-table.js';
import { initActionBar } from '../components/action-bar.js';
import { initNetdiskToolbar } from '../components/toolbar.js';
import { NETDISK_SOURCE } from '../features/data-sources.js';
import { set } from '../store.js';

/**
 * Initialize the netdisk view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initNetdiskView(container) {
  const toolbar = document.createElement('div');
  toolbar.id = 'netdisk-toolbar';
  container.appendChild(toolbar);
  initNetdiskToolbar(toolbar);

  const tableHost = document.createElement('div');
  tableHost.id = 'netdisk-content';
  container.appendChild(tableHost);
  const tableCleanup = initDataTable(tableHost, NETDISK_SOURCE);

  const actionBar = document.createElement('div');
  actionBar.id = 'netdisk-action-bar';
  container.appendChild(actionBar);
  const barCleanup = initActionBar(actionBar, NETDISK_SOURCE);

  return () => {
    tableCleanup();
    barCleanup();
    set('viewMode', 'files');
  };
}