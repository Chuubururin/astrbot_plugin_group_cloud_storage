/**
 * Files view (T-1) - group files tab.
 *
 * Assembles the files toolbar, breadcrumb+keyed table, action bar and
 * tag cloud over the GROUP_SOURCE adapter. Requirements covered:
 *  - five upload sources (local/URL/netdisk/album/essence, N-06)
 *  - three-tier refresh (groups / current group files / full rescan)
 *  - 13-class type chips + derived status chips (N-01/N-02)
 *  - full-filename search, folder rows + ../ up-level (N-03/N-08)
 *  - big-file/volume/long-asset badges via row fields (L-1, N-10)
 *
 * @module views/files
 */

import { initDataTable } from '../components/data-table.js';
import { initActionBar } from '../components/action-bar.js';
import { initFilesToolbar } from '../components/toolbar.js';
import { GROUP_SOURCE } from '../features/data-sources.js';

/**
 * Initialize the files view (mounts toolbar + table + tag cloud).
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initFilesView(container) {
  const toolbar = document.createElement('div');
  toolbar.id = 'files-toolbar';
  container.appendChild(toolbar);
  initFilesToolbar(toolbar);

  // The data table owns the single #tagcloud slot for group files; keeping
  // a second tag-cloud in the view would duplicate it for no benefit.
  const tableHost = document.createElement('div');
  tableHost.id = 'files-content';
  container.appendChild(tableHost);
  const tableCleanup = initDataTable(tableHost, GROUP_SOURCE);

  const actionBar = document.createElement('div');
  actionBar.id = 'file-action-bar';
  container.appendChild(actionBar);
  const barCleanup = initActionBar(actionBar, GROUP_SOURCE);

  return () => {
    tableCleanup();
    barCleanup();
  };
}