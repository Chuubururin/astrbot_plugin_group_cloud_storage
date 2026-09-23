/**
 * Resource table - the single list renderer for files / albums / essence /
 * netdisk.
 *
 * The table is driven entirely by a DataSource adapter: every source owns
 * its page key and type-filter key, so tabs never share pagination or chip
 * state accidentally. Rendering stays keyed and rAF-batched via
 * features/file-rows.js; a per-topic sequence guard drops stale responses.
 *
 * @module components/data-table
 */

import { getState, set, subscribe, nextSeq, isStale } from '../store.js';
import { DEFAULT_PAGE_SIZE } from '../constants.js';
import { getIcon } from '../icons.js';
import { applyLocalFilterSort, extTypeMap, netdiskTypeMap } from '../features/data-sources.js';
import { attachMarquee } from '../features/marquee-select.js';
import { renderBreadcrumb, renderTagCloud } from './breadcrumb.js';
import {
  renderRows, updateCheckboxes, syncSelectAll,
  updatePagination,
} from '../features/file-rows.js';
import { escapeHtml } from '../utils/helpers.js';
import { toast } from './toast.js';

const PREFIX = { group: 'file', album: 'album', essence: 'essence', netdisk: 'netdisk' };
const TOPIC = { group: 'files', album: 'albums', essence: 'essence', netdisk: 'netdisk' };
// Page-size choices offered by the pager (the live value lives in the store).
const PAGE_SIZES = [10, 24, 50, 100];

/** Build one pane of the table markup (pane B stays hidden in single mode). */
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
 * @returns {function} cleanup
 */
export function initDataTable(container, source) {
  const prefix = PREFIX[source.id] || 'file';
  const topic = TOPIC[source.id] || 'files';
  const isGroup = source.id === 'group';
  const dual = getState().layout === 'dual';
  // The pager select mirrors the size the request actually carries.
  const pageSize = getState().filePageSize || DEFAULT_PAGE_SIZE;

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
          ${PAGE_SIZES.map((v) => `<option value="${v}"${v === pageSize ? ' selected' : ''}>${v}</option>`).join('')}
        </select>
      </label>
    </div>
  `;

  const page = () => getState()[source.pageKey] || 1;
  const typeFilter = () => (source.typeKey ? getState()[source.typeKey] : '');
  const queryFor = (st) => (source.id === 'album' ? st.albumQuery
    : (source.id === 'essence' ? st.essenceQuery : st.searchQuery));

  // Load is serialized with tail coalescing: a request arriving while one is
  // in flight only marks dirty and reruns once after completion, so
  // continuous paging/sorting/typing never piles up concurrent requests.
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
      renderErrorRow(e);
      // 403 语义优先看结构化字段（后端 error_response 的 message /
      // groups/open-state 的 reason），中文子串仅作最后兜底。
      const reason = String((e && (e.reason || e.code || e.message)) || e || '');
      if (isGroup && (reason.includes('离线') || reason.includes('解散'))) {
        toast(reason.includes('离线') ? '群归属账号离线，已回退全部群聚合视图' : '该群已解散或不可访问，已回退全部群聚合视图', 'warn');
        // 回退必须同时清掉群与目录上下文（残留的 folder 会继续过滤聚合视图）；
        // 但已在聚合视图时不能再 set，否则会再次触发 currentGroup 订阅 ->
        // load() -> 再次失败，形成请求风暴。
        if (getState().currentGroup) set('currentGroup', '');
        set('folder', '');
        set('folderChain', []);
      } else {
        toast('加载列表失败', 'error');
      }
    } finally {
      loadingInFlight = false;
      if (!cancelled) set('loading', false);
      if (loadDirty && !cancelled) { loadDirty = false; load(); }
    }
  }

  /** Inline error state: a failed load must never keep stale rows on screen. */
  function renderErrorRow(e) {
    const paneA = container.querySelector('.file-tbody[data-pane="a"]');
    const paneB = container.querySelector('.file-tbody[data-pane="b"]');
    if (!paneA) return;
    paneA.innerHTML = '';
    if (paneB) paneB.innerHTML = '';
    const tr = document.createElement('tr');
    tr.dataset.key = 'load-error'; tr.dataset.dir = '1';
    tr.innerHTML = `<td colspan="6" class="empty-hint">加载失败：${escapeHtml(String(e && e.message || e || '网络错误'))}
      <button class="load-retry" type="button">重试</button></td>`;
    tr.querySelector('.load-retry').addEventListener('click', () => { tr.remove(); load(); });
    paneA.appendChild(tr);
  }

  async function doLoad() {
    const st = getState();
    // An empty group means the aggregated all-groups view.
    const seq = nextSeq(topic);
    // 只有群文件有 created_at 列；其余源（网盘走本地排序）用共享默认键
    // 会让首屏排序变成空操作，因此它们回退到 modified（与表头一致）。
    const sort = (!st.fileSort || (st.fileSort.by === 'created_at' && !isGroup))
      ? { by: isGroup ? 'created_at' : 'modified', dir: st.fileSort?.dir || 'desc' }
      : st.fileSort;
    const data = await source.list(st, {
      page: page(),
      page_size: st.filePageSize || DEFAULT_PAGE_SIZE,
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
        // Netdisk has its own classification map (text/audio/video/image/other)
        source.id === 'netdisk' ? netdiskTypeMap() : extTypeMap(st.extTypes),
      );
    }

    renderBreadcrumb(source, `${prefix}-breadcrumb`);
    renderTagCloud(data.tags);
    renderRows(container, source, rows, data.folders || []);
    updatePagination(container, source, prefix);
    syncSelectAll(source);
  }

  load();

  const subs = [
    subscribe(`refresh:${topic}`, load),
    subscribe(source.selectedKey, () => { updateCheckboxes(source); syncSelectAll(source); }),
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
      // 切群必须清选区：旧群的选中 id 对应的行已不在列表里，残留会让
      // 操作条显示"已选 N 项"而命令层按 rows 过滤后什么都选不中。
      set('fileSelected', new Set());
      load();
    }));
  } else if (source.id === 'netdisk') {
    subs.push(subscribe('netdiskPath', () => {
      set('netdiskPage', 1);
      // 切目录同样必须清选区：网盘的 rowKey 是 remote_path，旧目录的 key
      // 已不在列表里，残留会让批量删除把旧目录路径一并提交。
      set('netdiskSelected', new Set());
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

  // Select-all across both panes: incremental add/delete over the current
  // page (folder rows have no checkbox), so cross-page picks survive.
  container.querySelectorAll('.file-select-all').forEach((el) => {
    el.addEventListener('change', (e) => {
      const cur = new Set(getState()[source.selectedKey] || []);
      for (const f of getState()[source.itemsKey] || []) {
        if (f.is_dir) continue;
        const key = source.rowKey(f);
        if (e.target.checked) cur.add(key); else cur.delete(key);
      }
      source.selection.setMany([...cur]);
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
      // 目录行没有复选框（选中态不可见），网盘目录 key 又是 remote_path，
      // 会被批量删除当目录删掉 -> 与 canStart 用同一套行过滤。
      rowFilter: (r) => r.dataset.dir !== '1',
    });
  }

  // Pagination controls.
  container.querySelector(`#${prefix}-prev`)?.addEventListener('click', () => {
    if (page() > 1) { set(source.pageKey, page() - 1); load(); }
  });
  container.querySelector(`#${prefix}-next`)?.addEventListener('click', () => {
    const st = getState();
    const max = Math.ceil((st[source.totalKey] || 0) / (st.filePageSize || DEFAULT_PAGE_SIZE)) || 1;
    if (page() < max) { set(source.pageKey, page() + 1); load(); }
  });
  container.querySelector(`#${prefix}-page-size`)?.addEventListener('change', (e) => {
    const size = parseInt(e.target.value, 10) || DEFAULT_PAGE_SIZE;
    set('filePageSize', size);
    set(source.pageKey, 1);
    e.target.value = String(size); // 回写：显示值必须等于实际请求值
    load();
  });

  // Layout preference: pane B visibility follows the persisted mode.
  function applyLayoutMode() {
    const dualMode = getState().layout === 'dual';
    const grid = container.querySelector('.table-grid');
    const paneB = container.querySelector('.table-b');
    if (grid) grid.classList.toggle('single', !dualMode);
    if (paneB) paneB.classList.toggle('hidden', !dualMode);
    // 行是按 layout 对半拆进两个 pane 的：只切 class 会让 pane B 残留或
    // 缺失半页行 -> 必须按新分栏重排（load 有尾部合并，不会请求风暴）。
    load();
  }

  return () => { cancelled = true; subs.forEach((u) => u()); detachMarquee(); };
}
