/**
 * Group table - the groups tab shell (T-6): toolbar, dual-pane table,
 * pager, selection and event wiring. The data layer (sort/filter/page/
 * row building) lives in features/group-data.js and the bulk actions in
 * features/group-actions.js; this module stays within the line budget by
 * delegating to them.
 *
 * Requirements: only online-account groups qualify (L-4 - offline or
 * switched accounts are hidden backend-side, the list decays with scans);
 * row click sets the file context and opens the files tab.
 *
 * @module components/group-table
 */

import { getState, set, subscribe, refresh } from '../store.js';
import { getIcon } from '../icons.js';
import { attachMarquee } from '../features/marquee-select.js';
import {
  groupSlice, loadGroups, rerenderGroups, updateGroupCheckboxes, loadAccounts,
} from '../features/group-data.js';
import {
  handleBatchOps, handleRemove, handleRestore, handleToggleRemoved, handleMenuAction,
} from '../features/group-actions.js';
import { navigate } from '../router.js';
import { attachMenu } from './menu.js';

/**
 * Initialize the groups view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initGroupsView(container) {
  container.innerHTML = `
    <div class="groups-toolbar toolbar">
      <div class="toolbar-left">
        <select id="file-group-select" title="查看的群（B1：全部群=聚合视图；选择具体群即设置文件页群上下文）">
          <option value="">全部群</option>
        </select>
        <select id="account-filter" title="账号筛选">
          <option value="">全部账号</option>
        </select>
        <button id="btn-batch" class="primary" title="批量操作">${getIcon('EDIT', 13)} 批量操作</button>
        <button id="btn-remove" class="danger" title="移除管理">${getIcon('DELETE', 13)} 移除管理</button>
        <button id="btn-restore" class="primary hidden" title="恢复管理">恢复管理</button>
        <button id="btn-removed" title="查看已移除群">已移除群</button>
        <span class="toolbar-menu">
          <button id="btn-group-more">群操作${getIcon('CHEVRON_DOWN', 10)}</button>
          <div class="menu-box hidden" id="group-more-menu">
            <button class="menu-item" data-act="sync">群信息同步（增量）</button>
            <button class="menu-item" data-act="sort-label">按编号排序</button>
            <button class="menu-item" data-act="auto-label">补编号</button>
            <button class="menu-item" data-act="clear-labels">清除编号</button>
            <button class="menu-item" data-act="up">上移选中</button>
            <button class="menu-item" data-act="down">下移选中</button>
          </div>
        </span>
      </div>
      <div class="toolbar-right">
        <span id="group-count">0 个群</span>
      </div>
    </div>
    <div class="table-grid ${getState().layout === 'single' ? 'single' : ''}">
      ${paneHtml()}
      ${paneHtml('table-b' + (getState().layout === 'single' ? ' hidden' : ''))}
    </div>
    <div class="pager">
      <button id="g-prev">${getIcon('ARROW_LEFT', 12)}</button>
      <span id="g-page-info"></span>
      <button id="g-next">${getIcon('CHEVRON_RIGHT', 12)}</button>
      <label class="pager-label">每页
        <select id="group-page-size">
          <option value="10" selected>10</option>
          <option value="50">50</option>
          <option value="100">100</option>
          <!-- 2026-09-03 整改（S4）：每页含「全部」 -->
          <option value="0">全部</option>
        </select>
      </label>
    </div>
  `;

  /** @type {Set<string>} selected group ids of this view session */
  const selectedGroups = new Set();

  loadGroups(selectedGroups);
  loadAccounts();

  const subs = [
    subscribe('refresh:groups', () => loadGroups(selectedGroups)),
    subscribe('currentGroup', () => {
      const sel = container.querySelector('#file-group-select');
      if (sel) sel.value = getState().currentGroup || '';
    }),
    subscribe('layout', () => {
      const dual = getState().layout !== 'single';
      const grid = container.querySelector('.table-grid');
      const paneB = container.querySelector('.table-b');
      if (grid) grid.classList.toggle('single', !dual);
      if (paneB) paneB.classList.toggle('hidden', !dual);
      rerenderGroups(selectedGroups);
    }),
  ];

  // Select-all header (both panes).
  container.querySelectorAll('.group-select-all').forEach((el) => {
    el.addEventListener('change', (e) => {
      const { slice } = groupSlice();
      slice.forEach((g) => (e.target.checked
        ? selectedGroups.add(g.group_id)
        : selectedGroups.delete(g.group_id)));
      updateGroupCheckboxes(selectedGroups);
    });
  });

  // Sortable headers.
  container.querySelectorAll('th[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const { groupSort } = getState();
      const key = th.dataset.sort;
      const dir = groupSort.key === key && groupSort.dir === 'asc' ? 'desc' : 'asc';
      set('groupSort', { key, dir });
      rerenderGroups(selectedGroups);
    });
  });

  // Pagination.
  container.querySelector('#g-prev')?.addEventListener('click', () => {
    if (getState().groupPage > 1) {
      set('groupPage', getState().groupPage - 1);
      rerenderGroups(selectedGroups);
    }
  });
  container.querySelector('#g-next')?.addEventListener('click', () => {
    set('groupPage', getState().groupPage + 1);
    rerenderGroups(selectedGroups);
  });
  container.querySelector('#group-page-size')?.addEventListener('change', (e) => {
    set('groupPageSize', parseInt(e.target.value, 10));
    set('groupPage', 1);
    rerenderGroups(selectedGroups);
  });

  // Account filter (client-side over the full list).
  container.querySelector('#account-filter')?.addEventListener('change', (e) => {
    set('accountFilter', e.target.value);
    set('groupPage', 1);
    rerenderGroups(selectedGroups);
  });

  // Group context select: navigating to the files tab with a concrete group.
  container.querySelector('#file-group-select')?.addEventListener('change', (e) => {
    const gid = e.target.value;
    set('currentGroup', gid || '');
    set('filePage', 1);
    navigate('files');
  });

  // Management actions.
  container.querySelector('#btn-batch')?.addEventListener('click', () => handleBatchOps(selectedGroups));
  container.querySelector('#btn-remove')?.addEventListener('click', () => handleRemove(selectedGroups));
  container.querySelector('#btn-removed')?.addEventListener('click', () => handleToggleRemoved(selectedGroups));
  container.querySelector('#btn-restore')?.addEventListener('click', () => handleRestore(selectedGroups));

  // More menu: shared dropdown component (single global close handler).
  attachMenu(
    container.querySelector('#btn-group-more'),
    container.querySelector('#group-more-menu'),
  );
  container.querySelectorAll('#group-more-menu [data-act]').forEach((btn) => {
    btn.addEventListener('click', () => handleMenuAction(btn.dataset.act, selectedGroups));
  });

  // Marquee rectangle selection over the grid host.
  const wrap = container.querySelector('.table-grid');
  let detachMarquee = () => {};
  if (wrap) {
    detachMarquee = attachMarquee(wrap, {
      rowKeyAttr: 'key',
      getSelection: () => Array.from(selectedGroups),
      setSelection: (keys) => {
        selectedGroups.clear();
        for (const k of keys) selectedGroups.add(k);
        updateGroupCheckboxes(selectedGroups);
      },
    });
  }

  return () => {
    subs.forEach((u) => u());
    detachMarquee();
    selectedGroups.clear();
  };
}

/** One pane's table markup (name/role/space/album/essence/scan columns). */
function paneHtml(extraClass = '') {
  return `
    <div class="table-wrap ${extraClass}">
      <table class="compact group-table">
        <colgroup>
          <col style="width:18px" /><col style="width:auto" />
          <col style="width:64px" /><col style="width:24px" />
          <col style="width:28px" /><col style="width:74px" />
          <col style="width:24px" /><col style="width:24px" />
          <col style="width:56px" />
        </colgroup>
        <thead><tr>
          <th><input class="group-select-all" type="checkbox" title="全选本页" /></th>
          <th data-sort="group_name">群名称</th>
          <th data-sort="group_id">群号</th>
          <th data-sort="label">编号</th>
          <th>角色</th>
          <th data-sort="used_space">容量</th>
          <th>相册</th>
          <th>精华</th>
          <th data-sort="last_scan">最近扫描</th>
        </tr></thead>
        <tbody class="group-tbody" data-pane="${extraClass ? 'b' : 'a'}"></tbody>
      </table>
    </div>`;
}