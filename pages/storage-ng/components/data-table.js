/**
 * Resource table - the single list renderer for files / albums / essence /
 * netdisk (C1-C9, FE-18).
 *
 * The table is driven entirely by a DataSource adapter. Every source owns
 * its page key and type-filter key, so the tabs no longer share pagination
 * or chip state accidentally. Rendering stays keyed and rAF-batched via
 * features/file-rows.js and only data_changed-triggered refreshes reload
 * (FE-14); a per-topic sequence guard drops stale responses.
 *
 * @module components/data-table
 */

import { getState, set, subscribe, refresh, nextSeq, isStale } from '../store.js';
import { getIcon } from '../icons.js';
import { applyLocalFilterSort, netdiskTypeMap } from '../features/data-sources.js';
import { attachMarquee } from '../features/marquee-select.js';
import { renderBreadcrumb, renderTagCloud } from './breadcrumb.js';
import {
  extTypeMap, renderRows, updateCheckboxes, syncSelectAll,
  updatePagination,
} from '../features/file-rows.js';
import { toast } from './toast.js';

const PREFIX = { group: 'file', album: 'album', essence: 'essence', netdisk: 'netdisk' };
const TOPIC = { group: 'files', album: 'albums', essence: 'essence', netdisk: 'netdisk' };

/**
 * Build one pane of the table markup (pane B is hidden in single mode).
 * @param {string} prefix - DOM id prefix unique to the source
 * @param {boolean} isGroup - group files sort by created_at by default
 * @param {string} extraClass - 'table-b hidden' for the second pane
 * @param {string} pane - 'a'|'b'
 */
function paneHtml(prefix, isGroup, extraClass = '', pane = 'a') {
  return `
    <div class="table-wrap ${extraClass}">
      <table class="file-table">
        <colgroup>
          <col style="width:26px" /><col style="width:auto" />
          <col style="width:40px" /><col style="width:58px" />
          <col style="width:62px" /><col style="width:78px" />
        </colgroup>
        <thead><tr>
          <th class="col-chk"><input type="checkbox" class="file-select-all" /></th>
          <th data-sort="name">名称</th>
          <th>类型</th>
          <th data-sort="size">大小</th>
          <th>上传者</th>
          <th data-sort="${isGroup ? 'created_at' : 'modified'}">修改时间</th>
        </tr></thead>
        <tbody class="file-tbody" data-pane="${pane}"></tbody>
      </table>
    </div>
  `;
}

/**
 * Mount the resource table for a DataSource.
 * @param {HTMLElement} container - view-owned host element
 * @param {Object} source - DataSource adapter
 * @returns {function} cleanup
 */
export function initDataTable(container, source) {
  const prefix = PREFIX[source.id] || 'file';
  const topic = TOPIC[source.id] || 'files';
  const isGroup = source.id === 'group';
  const dual = getState().layout === 'dual';

  container.innerHTML = `
    <div id="${prefix}-breadcrumb" class="breadcrumb"></div>
    ${isGroup ? '<div id="tagcloud" class="tagcloud hidden"></div>' : ''}
    <div class="table-grid ${dual ? '' : 'single'}">
      ${paneHtml(prefix, isGroup)}
      ${paneHtml(prefix, isGroup, dual ? 'table-b' : 'table-b hidden', 'b')}
    </div>
    <div class="pager">
      <button id="${prefix}-prev">${getIcon('ARROW_LEFT', 12)}</button>
      <span id="${prefix}-page-info"></span>
      <button id="${prefix}-next">${getIcon('CHEVRON_RIGHT', 12)}</button>
      <label class="pager-label">每页
        <select id="${prefix}-page-size">
          <option value="10">10</option>
          <option value="24" ${source.id === 'netdisk' ? '' : 'selected'}>24</option>
          <option value="50" ${source.id === 'netdisk' ? 'selected' : ''}>50</option>
          <option value="100">100</option>
        </select>
      </label>
    </div>
  `;

  const page = () => getState()[source.pageKey] || 1;
  const typeFilter = () => (source.typeKey ? getState()[source.typeKey] : '');
  const queryFor = (st) => (
    source.id === 'album' ? st.albumQuery
      : (source.id === 'essence' ? st.essenceQuery : st.searchQuery)
  );

  // 2026-09-03 性能修复（P-1）：load 串行化 + 尾部合并——in-flight 期间的新请求
  // 只标记 dirty，完成后再跑一次（连续翻页/排序/击键不再并发堆积请求）。
  let loadingInFlight = false;
  let loadDirty = false;
  let cancelled = false;

  async function load() {
    if (cancelled) return;
    if (loadingInFlight) { loadDirty = true; return; }
    loadingInFlight = true;
    set('loading', true);
    try {
      await doLoad();
    } catch (e) {
      console.error('[data-table] load failed:', e);
      toast('加载列表失败', 'error');
    } finally {
      loadingInFlight = false;
      if (!cancelled) set('loading', false);
      if (loadDirty && !cancelled) { loadDirty = false; load(); }
    }
  }

  async function doLoad() {
    const st = getState();
    // D-3: an empty group means the aggregated all-groups view.
    const seq = nextSeq(topic);
    try {
      const sort = st.fileSort || { by: 'created_at', dir: 'desc' };
      const data = await source.list(st, {
        page: page(),
        page_size: st.filePageSize || 24,
        type: typeFilter(),
        folder: st.folder,
        q: queryFor(st),
        status: st.fileStatus,
        sort_by: sort.by,
        sort_dir: sort.dir,
      });
      if (isStale(topic, seq)) return; // superseded by a newer request

      set(source.itemsKey, data.items);
      set(source.totalKey, data.total);
      if (isGroup) {
        set('folders', data.folders || []);
        if (data.tags) set('tags', data.tags);
      }
      if (source.id === 'album') set('albumTagCloud', data.tags || []);
      if (source.id === 'essence') set('essenceTagCloud', data.tags || []);

      let rows = data.items;
      // Netdisk: no server-side filter/sort -> apply locally (N4a).
      if (!source.serverSort) {
        rows = applyLocalFilterSort(
          rows,
          { type: typeFilter(), sort_by: sort.by, sort_dir: sort.dir },
          // 2026-09-03 网盘独立分类映射（文本/音频/视频/图片/其他）
          source.id === 'netdisk' ? netdiskTypeMap() : extTypeMap(st.extTypes),
        );
      }

      renderBreadcrumb(source, `${prefix}-breadcrumb`);
      renderTagCloud(data.tags);
      renderRows(container, source, rows, data.folders || []);
      updatePagination(container, source, prefix);
      syncSelectAll(source);
    } catch (e) {
      throw e;
    }
  }

  load();

  const subs = [
    subscribe(`refresh:${topic}`, load),
    subscribe(source.selectedKey, () => {
      updateCheckboxes(source);
      syncSelectAll(source);
    }),
    subscribe('layout', applyLayoutMode),
  ];
  if (source.typeKey) subs.push(subscribe(source.typeKey, load));
  if (isGroup) {
    // fileStatus changes are followed by refresh('files') from the toolbar,
    // so no extra subscription is needed (avoids duplicate requests).
    subs.push(subscribe('currentGroup', () => {
      set('filePage', 1);
      set('folder', '');
      set('folderChain', []);
      load();
    }));
  } else if (source.id === 'netdisk') {
    subs.push(subscribe('netdiskPath', () => {
      set('netdiskPage', 1);
      load();
    }));
  } else if (source.id === 'album') {
    subs.push(subscribe('albumGroup', () => { set('albumPage', 1); load(); }));
  } else if (source.id === 'essence') {
    subs.push(subscribe('essenceGroup', () => { set('essencePage', 1); load(); }));
  }

  // Sortable headers (server for group/album/essence, local page for netdisk).
  container.querySelectorAll('th[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const { fileSort } = getState();
      const by = th.dataset.sort;
      const dir = fileSort.by === by && fileSort.dir === 'asc' ? 'desc' : 'asc';
      set('fileSort', { by, dir });
      set(source.pageKey, 1);
      load();
    });
  });

  // Select-all across both panes (folder rows excluded).
  container.querySelectorAll('.file-select-all').forEach((el) => {
    el.addEventListener('change', (e) => {
      const items = getState()[source.itemsKey] || [];
      const fileRows = items.filter((f) => !f.is_dir);
      if (e.target.checked) source.selection.setMany(fileRows.map(source.rowKey));
      else source.selection.clear();
    });
  });

  // Marquee rectangle selection over the grid host.
  const wrap = container.querySelector('.table-grid');
  let detachMarquee = () => {};
  if (wrap) {
    detachMarquee = attachMarquee(wrap, {
      rowKeyAttr: 'key',
      getSelection: () => Array.from(getState()[source.selectedKey] || []),
      setSelection: (keys) => source.selection.setMany(keys),
      canStart: (ev) => !ev.target.closest('tr[data-dir="1"]'),
    });
  }

  // Pagination controls.
  container.querySelector(`#${prefix}-prev`)?.addEventListener('click', () => {
    if (page() > 1) { set(source.pageKey, page() - 1); load(); }
  });
  container.querySelector(`#${prefix}-next`)?.addEventListener('click', () => {
    const st = getState();
    const max = Math.ceil((st[source.totalKey] || 0) / (st.filePageSize || 24)) || 1;
    if (page() < max) { set(source.pageKey, page() + 1); load(); }
  });
  container.querySelector(`#${prefix}-page-size`)?.addEventListener('change', (e) => {
    set('filePageSize', parseInt(e.target.value, 10));
    set(source.pageKey, 1);
    load();
  });

  // Layout preference: pane B visibility follows the persisted mode.
  function applyLayoutMode() {
    const dualMode = getState().layout === 'dual';
    const grid = container.querySelector('.table-grid');
    const paneB = container.querySelector('.table-b');
    if (grid) grid.classList.toggle('single', !dualMode);
    if (paneB) paneB.classList.toggle('hidden', !dualMode);
  }

  return () => { cancelled = true; subs.forEach((u) => u()); detachMarquee(); };
}
