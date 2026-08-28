/**
 * Breadcrumb + tag cloud (N-08 file-manager paradigm).
 *
 * The list views follow the file-manager convention: a breadcrumb strip
 * at the top renders the folder chain (group files: flat single-level
 * folders; netdisk: arbitrary path segments), plus a tag cloud used by
 * the files/albums/essence tabs for custom-tag filtering (N-04/N-05).
 *
 * @module components/breadcrumb
 */

import { getState, set, refresh } from '../store.js';
import { getIcon } from '../icons.js';
import { escapeHtml } from '../utils/helpers.js';

/**
 * Render the breadcrumb of a data source (group folder chain or netdisk path).
 * @param {Object} source - DataSource adapter
 * @param {string} containerId - element id hosting the breadcrumb
 */
export function renderBreadcrumb(source, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const crumbs = [];
  if (source.id === 'group') {
    crumbs.push({ label: '全部目录', folder: '' });
    (getState().folderChain || []).forEach((c) => crumbs.push({ label: c.name, folder: c.id || c.name }));
  } else if (source.id === 'netdisk') {
    crumbs.push({ label: '根目录', path: '/' });
    let acc = '';
    (getState().netdiskPath || '/').split('/').filter(Boolean).forEach((seg) => {
      acc += `/${seg}`;
      crumbs.push({ label: seg, path: acc });
    });
  } else {
    // Albums/essence are flat cloud indexes: a single inert root crumb.
    crumbs.push({ label: source.id === 'album' ? '全部相册' : '全部精华' });
  }
  el.innerHTML = crumbs.map((c, i) => {
    const active = i === crumbs.length - 1;
    const attr = source.id === 'group'
      ? `data-folder="${escapeHtml(c.folder || '')}"`
      : (source.id === 'netdisk' ? `data-path="${escapeHtml(c.path)}"` : '');
    return `${i > 0 ? '<span class="crumb-sep">/</span>' : ''}` +
      `<button class="crumb ${active ? 'active' : ''}" ${attr}>` +
      `${i === 0 ? getIcon('FOLDER', 12) + ' ' : ''}${escapeHtml(c.label)}</button>`;
  }).join('');

  el.querySelectorAll('[data-folder]').forEach((btn) => {
    btn.addEventListener('click', () => {
      set('folder', btn.dataset.folder);
      set('folderChain', btn.dataset.folder ? [{ name: btn.dataset.folder }] : []);
      set('filePage', 1);
      refresh('files');
    });
  });
  el.querySelectorAll('[data-path]').forEach((btn) => {
    btn.addEventListener('click', () => {
      set('netdiskPath', btn.dataset.path);
      set('netdiskPage', 1);
      refresh('netdisk');
    });
  });
}

/**
 * Render a tag cloud into a container; clicks toggle the module's tag filter.
 *
 * @param {Array<{tag: string, count: number}>} tags - tag counts
 * @param {Object} opts - {container, tagKey (store key), topic (refresh topic)}
 */
export function renderTagCloud(tags, opts = {}) {
  const el = opts.container || document.getElementById('tagcloud');
  const key = opts.tagKey || 'tagFilter';
  if (!el) return;
  if (!tags || tags.length === 0) { el.classList.add('hidden'); return; }
  const active = getState()[key] || '';
  el.classList.remove('hidden');
  el.innerHTML = tags.map((t) =>
    `<button class="tag ${t.tag === active ? 'hit' : ''}" data-tag="${escapeHtml(t.tag)}">` +
    `${escapeHtml(t.tag)} (${t.count})</button>`
  ).join('');
  el.querySelectorAll('.tag').forEach((btn) => {
    btn.addEventListener('click', () => {
      set(key, btn.dataset.tag === active ? '' : btn.dataset.tag);
      const pageKey = { files: 'filePage', albums: 'albumPage', essence: 'essencePage', netdisk: 'netdiskPage' }[opts.topic] || 'filePage';
      set(pageKey, 1);
      refresh(opts.topic || 'files');
    });
  });
}