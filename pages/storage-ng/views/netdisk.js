/**
 * Netdisk view  - OpenList storage tab.
 *
 * Layout:
 *   1. netdisk toolbar (upload local/URL, root, mkdir, type chips)
 *   2. unified data table over the netdisk source (browse, local type
 *      filter/sort, ../ up-level navigation)
 *   3. action bar (link, download, rename, tags, delete, distribute
 *      local/group/album/essence)
 *   4. bridge panel (transfer task table with direction tabs, failed-task
 *      retry / pending-task cancel, bridge health strip, OpenList
 *      connection config modal)
 *
 * The bridge panel is mounted here (not in the tasks tab): its rows are
 * OpenList-side archive_map entries carrying remote_path/direction, which
 * the op_queue ledger does not show, and bridge/retry + bridge/cancel are
 * its only UI. OpenList keys are also editable from the config center;
 * the panel's config modal exposes the bridge-scoped subset.
 *
 * @module views/netdisk
 */

import { initDataTable } from '../components/data-table.js';
import { initActionBar } from '../components/action-bar.js';
import { initBridgePanel } from '../components/bridge-panel.js';
import { initNetdiskToolbar } from '../components/toolbar.js';
import { NETDISK_SOURCE } from '../features/data-sources.js';

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

  const bridgeHost = document.createElement('div');
  bridgeHost.id = 'netdisk-bridge-panel';
  container.appendChild(bridgeHost);
  const bridgeCleanup = initBridgePanel(bridgeHost);

  return () => {
    tableCleanup();
    barCleanup();
    bridgeCleanup();
  };
}