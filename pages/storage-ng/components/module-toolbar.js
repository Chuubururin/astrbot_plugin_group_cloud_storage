/**
 * Module toolbar - shared albums/essence toolbar .
 *
 * The albums and essence tabs used to carry two near-identical toolbar
 * copies (upload entry, group focus select, two-tier refresh menu,
 * search box, count badge). They now share one implementation driven by
 * a module spec; this keeps the per-tab views thin and removes the
 * duplicated group-focus/refresh/search wiring.
 *
 * @module components/module-toolbar
 */

import { getState, set, refresh, subscribe } from '../store.js';
import { API } from '../api.js';
import { getIcon } from '../icons.js';
import { debounce, escapeHtml } from '../utils/helpers.js';
import { mutate } from '../utils/mutate.js';
import { attachMenu } from './menu.js';
import { toast } from './toast.js';
import {
  showAlbumUploadModal, handleAlbumFileUpload,
  showEssenceUploadModal, handleEssenceFileUpload,
} from '../features/ingest.js';

/** Module specs: everything the shared toolbar needs to differ per tab. */
export const MODULE_TOOLBAR_SPECS = {
  album: {
    id: 'album',
    topic: 'albums',
    groupKey: 'albumGroup',
    queryKey: 'albumQuery',
    searchPlaceholder: '搜索相册名称/描述...',
    countSuffix: ' 个相册',
    refreshHint: '云端刷新（全部群，含相册重采集）',
    cloudScope: 'albums',
    openUpload: showAlbumUploadModal,
    handleUpload: handleAlbumFileUpload,
    accept: 'image/*,video/*',
  },
  essence: {
    id: 'essence',
    topic: 'essence',
    groupKey: 'essenceGroup',
    queryKey: 'essenceQuery',
    searchPlaceholder: '搜索精华内容...',
    countSuffix: ' 条精华',
    refreshHint: '云端刷新（全部群，含精华重采集）',
    cloudScope: 'essence',
    openUpload: showEssenceUploadModal,
    handleUpload: handleEssenceFileUpload,
    accept: '.txt,.md,.docx,.pdf',
  },
};

/**
 * Mount the shared module toolbar (upload + group focus + two-tier
 * refresh + search). The count badge (id = <mod>-count) is updated by
 * the owning view.
 * @param {HTMLElement} container
 * @param {'album'|'essence'} modId
 * @returns {function} cleanup
 */
export function initModuleToolbar(container, modId) {
  const mod = MODULE_TOOLBAR_SPECS[modId];
  container.className = 'toolbar';
  container.innerHTML = `
    <div class="toolbar-left">
      <button id="${mod.id}-upload" class="primary" title="上传">${getIcon('UPLOAD', 14)} 上传</button>
      <select id="${mod.id}-group" class="group-focus" title="群聚焦（未选=全部群聚合）"></select>
      <span class="toolbar-menu">
        <button id="${mod.id}-refresh-menu" class="icon-btn" title="刷新（菜单两档）">${getIcon('REFRESH', 14)}<span class="caret"></span></button>
        <div class="menu-box hidden" id="${mod.id}-refresh-menu-box">
          <button class="menu-item" data-act="list">刷新列表（当前群/全部）</button>
          <button class="menu-item" data-act="cloud">${mod.refreshHint}</button>
        </div>
      </span>
      <input type="search" id="${mod.id}-search" placeholder="${mod.searchPlaceholder}" value="${getState()[mod.queryKey] || ''}" />
      <input type="file" id="${mod.id}-file" accept="${mod.accept}" multiple style="display:none" />
    </div>
    <div class="toolbar-right">
      <span id="${mod.id}-count" class="count-badge"></span>
    </div>
  `;

  // Upload entry: module source modal; local media flows through the
  // hidden input -> module upload handler.
  container.querySelector(`#${mod.id}-upload`)?.addEventListener('click', () => (
    mod.openUpload(container.querySelector(`#${mod.id}-file`))
  ));
  container.querySelector(`#${mod.id}-file`)?.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
      mod.handleUpload(e.target.files);
      e.target.value = '';
    }
  });

  // Group focus: '' = aggregated view over all groups .
  renderGroupFocus(container.querySelector(`#${mod.id}-group`), mod);
  const unsub = subscribe('groups', () => renderGroupFocus(container.querySelector(`#${mod.id}-group`), mod));

  // Two-tier refresh menu (list vs cloud rescan).
  attachMenu(
    container.querySelector(`#${mod.id}-refresh-menu`),
    container.querySelector(`#${mod.id}-refresh-menu-box`),
  );
  container.querySelectorAll(`#${mod.id}-refresh-menu-box [data-act]`).forEach((btn) => {
    btn.addEventListener('click', () => handleModuleRefresh(btn.dataset.act, mod));
  });

  // Independent per-module search  with debounce.
  container.querySelector(`#${mod.id}-search`)?.addEventListener('input', debounce(() => {
    set(mod.queryKey, container.querySelector(`#${mod.id}-search`).value);
    set(mod.id === 'album' ? 'albumPage' : 'essencePage', 1);
    refresh(mod.topic);
  }, 260));

  return unsub;
}

/** Group focus select options from the groups state; '' = all groups. */
function renderGroupFocus(selectEl, mod) {
  if (!selectEl) return;
  const groups = getState().groups || [];
  const cur = getState()[mod.groupKey] || '';
  if (!groups.length) {
    selectEl.innerHTML = '<option value="">请先在群组 Tab 加载群列表</option>';
    selectEl.disabled = true;
    return;
  }
  selectEl.disabled = false;
  selectEl.innerHTML = '<option value="">全部群（聚合）</option>' + groups.map((g) =>
    `<option value="${g.group_id}" ${g.group_id === cur ? 'selected' : ''}>` +
    `${escapeHtml(g.group_name || String(g.group_id))} (${g.group_id})</option>`).join('');
  selectEl.onchange = () => {
    set(mod.groupKey, selectEl.value);
    set(mod.id === 'album' ? 'albumPage' : 'essencePage', 1);
    refresh(mod.topic);
  };
}

async function handleModuleRefresh(act, mod) {
  const { [mod.groupKey]: focusGroup } = getState();
  if (act === 'list') {
    refresh(mod.topic);
    toast('列表已刷新', 'success');
    return;
  }
  // Cloud rescan including album/essence recollection. When a module group
  // focus is set the scan narrows to that group (scope + group_ids);
  // otherwise all managed groups (mode=all).
  if (focusGroup) {
    await mutate('云端刷新', API.GROUPS.SCAN,
      { scope: mod.cloudScope, mode: 'range', group_ids: [focusGroup] },
      { successText: '云端刷新已启动（当前群）' });
  } else {
    await mutate('云端刷新', API.GROUPS.SCAN, { mode: 'all' },
      { successText: '云端刷新已启动（全部群）' });
  }
}