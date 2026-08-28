/**
 * Netdisk view (T-4) - OpenList storage tab (2026-09-03 整改 S2)。
 *
 * Layout:
 *   1. netdisk toolbar (upload local/URL, root, mkdir, type chips)
 *   2. unified data table over the netdisk source (browse, local type
 *      filter/sort, ../ up-level navigation)
 *   3. action bar (link, download, rename, tags, delete, distribute
 *      local/group/album/essence)
 *
 * 已移除（S2）：桥接传输面板（任务统一走任务 Tab）、深度索引入口（无意义）、
 * 重复的 transfer-in 入口（网盘→群文件=分发 target=group）。OpenList 连接配置
 * 归属配置 Tab（config 中心分组）；传输任务归属任务 Tab（台账四动作）。
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