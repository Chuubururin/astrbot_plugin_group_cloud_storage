/**
 * Albums view  - group albums tab.
 *
 * Uses the shared module toolbar (upload / group focus / two-tier refresh
 * / title-desc search) plus the unified data table over the album source,
 * its action bar (gallery / detail / distribute) and the module-isolated
 * tag cloud (W-9). Media preview (image gallery + video keyframe GIFs)
 * opens on double-click through features/preview.js.
 *
 * @module views/albums
 */

import { initDataTable } from '../components/data-table.js';
import { initActionBar } from '../components/action-bar.js';
import { initModuleToolbar } from '../components/toolbar.js';
import { ALBUM_SOURCE } from '../features/data-sources.js';
import { renderTagCloud } from '../components/breadcrumb.js';
import { subscribe } from '../store.js';

/**
 * Initialize the albums view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initAlbumsView(container) {
  const toolbar = document.createElement('div');
  container.appendChild(toolbar);
  const unsubToolbar = initModuleToolbar(toolbar, 'album');

  const tagCloudEl = document.createElement('div');
  tagCloudEl.className = 'tagcloud hidden';
  container.appendChild(tagCloudEl);
  const tagUnsub = subscribe('albumTagCloud', (tags) => {
    renderTagCloud(tags || [], { container: tagCloudEl, tagKey: 'albumTagFilter', topic: 'albums' });
    tagCloudEl.classList.toggle('hidden', !tags || tags.length === 0);
  });

  const tableHost = document.createElement('div');
  container.appendChild(tableHost);
  const tableCleanup = initDataTable(tableHost, ALBUM_SOURCE);

  const actionBar = document.createElement('div');
  // Action-bar container id, locatable by the E2E probe (same as the
  // files/netdisk views).
  actionBar.id = 'album-action-bar';
  container.appendChild(actionBar);
  const barCleanup = initActionBar(actionBar, ALBUM_SOURCE);

  // Count badge lives in the module toolbar (id = album-count).
  const countUnsub = subscribe(ALBUM_SOURCE.totalKey, (total) => {
    const el = document.getElementById('album-count');
    if (el) el.textContent = `${total || 0} 个相册`;
  });

  return () => {
    unsubToolbar();
    tagUnsub();
    tableCleanup();
    barCleanup();
    countUnsub();
  };
}