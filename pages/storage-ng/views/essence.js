/**
 * Essence view  - essence messages tab.
 *
 * The shared module toolbar (upload: browser input / document file / URL
 * read) + unified data table over the essence source + action bar
 * (view / detail / distribute local-copy-netdisk-group) + module-isolated
 * tag cloud (W-9). Full-text preview on double-click, and a character
 * counter summed from the listed row sizes (the backend stores
 * `size = len(text)`, so the badge is a character count; exact totals
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
  // Action-bar container id, locatable by the E2E probe (same as the
  // files/netdisk views).
  actionBar.id = 'essence-action-bar';
  container.appendChild(actionBar);
  const barCleanup = initActionBar(actionBar, ESSENCE_SOURCE);

  const countUnsub = subscribe(ESSENCE_SOURCE.totalKey, (total) => {
    const el = document.getElementById('essence-count');
    if (el) el.textContent = `${total || 0} 条精华`;
  });
  // 字符统计：后端精华行的 size 就是字符数（size = len(text)，不是字节数）。
  // 不能走 formatSize——它的最小单位是 MB 且下限 0.1 MB，会把 <4000 字符
  // 硬限制区间内的任何文本都压成「共 0.1 MB 文本」，既不是字符数也分不出
  // 100 字与 3900 字。
  const charsUnsub = subscribe(ESSENCE_SOURCE.itemsKey, (items) => {
    const total = (items || []).reduce((s, it) => s + (Number(it.size) || 0), 0);
    charsEl.textContent = total ? `共 ${total} 字符` : '';
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