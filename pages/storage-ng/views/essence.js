/**
 * Essence view (T-3) - essence messages tab.
 *
 * The shared module toolbar (upload: browser input / document file / URL
 * read) + unified data table over the essence source + action bar
 * (view / detail / distribute local-copy-netdisk-group) + module-isolated
 * tag cloud (W-9). Full-text preview on double-click, and a character
 * counter derived from the listed text sizes (字符统计; the real totals
 * come from the full-text viewer).
 *
 * @module views/essence
 */

import { initDataTable } from '../components/data-table.js';
import { initActionBar } from '../components/action-bar.js';
import { initModuleToolbar } from '../components/toolbar.js';
import { ESSENCE_SOURCE } from '../features/data-sources.js';
import { renderTagCloud } from '../components/breadcrumb.js';
import { subscribe } from '../store.js';
import { formatSize } from '../utils/helpers.js';

/**
 * Initialize the essence view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initEssenceView(container) {
  const toolbar = document.createElement('div');
  container.appendChild(toolbar);
  const unsubToolbar = initModuleToolbar(toolbar, 'essence');

  // Character-stat badge rendered next to the count badge.
  const charsEl = document.createElement('span');
  charsEl.id = 'essence-chars';
  charsEl.className = 'count-badge';
  toolbar.querySelector('#essence-count')?.after(charsEl);

  const tagCloudEl = document.createElement('div');
  tagCloudEl.className = 'tagcloud hidden';
  container.appendChild(tagCloudEl);
  const tagUnsub = subscribe('essenceTagCloud', (tags) => {
    renderTagCloud(tags || [], { container: tagCloudEl, tagKey: 'essenceTagFilter', topic: 'essence' });
    tagCloudEl.classList.toggle('hidden', !tags || tags.length === 0);
  });

  const tableHost = document.createElement('div');
  container.appendChild(tableHost);
  const tableCleanup = initDataTable(tableHost, ESSENCE_SOURCE);

  const actionBar = document.createElement('div');
  // 2026-09-03 核对补写：动作栏容器 id（E2E 探针可定位，与 files/netdisk 视图一致）
  actionBar.id = 'essence-action-bar';
  container.appendChild(actionBar);
  const barCleanup = initActionBar(actionBar, ESSENCE_SOURCE);

  const countUnsub = subscribe(ESSENCE_SOURCE.totalKey, (total) => {
    const el = document.getElementById('essence-count');
    if (el) el.textContent = `${total || 0} 条精华`;
  });
  // Approximate character stats from listed sizes (exact totals in viewer).
  const charsUnsub = subscribe(ESSENCE_SOURCE.itemsKey, (items) => {
    const total = (items || []).reduce((s, it) => s + (it.size || 0), 0);
    charsEl.textContent = total ? `共 ${formatSize(total)} 文本` : '';
  });

  return () => {
    unsubToolbar();
    tagUnsub();
    tableCleanup();
    barCleanup();
    countUnsub();
    charsUnsub();
  };
}