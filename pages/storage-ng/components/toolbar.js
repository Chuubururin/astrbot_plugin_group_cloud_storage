/**
 * Toolbars - files and netdisk tabs.
 *
 *  - files   : upload (five sources), URL/text ingest, three-tier
 *                   refresh menu, 13-class chips, status chips, search
 *  - netdisk : refresh, local/URL upload, root/mkdir/deep-index,
 *                   type chips (local filter, folder excluded)
 *
 * The albums/essence shared toolbar (group focus / two-tier refresh /
 * search / upload) lives in module-toolbar.js and is re-exported here so
 * the owning views import one module.
 *
 * @module components/toolbar
 */

import { getState, set, refresh } from '../store.js';
import { API } from '../api.js';
import { getIcon } from '../icons.js';
import { debounce } from '../utils/helpers.js';
import { mutate } from '../utils/mutate.js';
import { attachMenu } from './menu.js';
import { promptEx, showFormModal } from './modal.js';
import { toast } from './toast.js';
import {
  showUploadSourceModal,
  handleNetdiskUploadLocal,
} from '../features/ingest.js';
import { handleFileUpload, resolveUploadGroup } from '../features/upload.js';
import {
  openGroupFileToNetdisk, openAlbumToNetdisk, openEssenceToNetdisk,
} from '../features/cross-upload.js';
import { openNetdiskUrlUpload } from '../features/netdisk-ops.js';

export { initModuleToolbar, MODULE_TOOLBAR_SPECS } from './module-toolbar.js';

// 13-class classification chips (folder is a row class).
// Netdisk uses its own classification (not the group-file 13 classes):
// text / audio / video / image / other.
export const NETDISK_CHIPS = [
  { value: '', label: '全部' },
  { value: 'text', label: '文本' },
  { value: 'audio', label: '音频' },
  { value: 'video', label: '视频' },
  { value: 'image', label: '图片' },
  { value: 'other', label: '其他' },
];

export const TYPE_CHIPS = [
  { value: '', label: '全部' },
  { value: 'document', label: '文稿' },
  { value: 'pdf', label: 'PDF' },
  { value: 'spreadsheet', label: '表格' },
  { value: 'slide', label: '幻灯片' },
  { value: 'online_doc', label: '在线文档' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'audio', label: '音频' },
  { value: 'archive', label: '压缩包' },
  { value: 'installer', label: '安装包' },
  { value: 'flash', label: '闪传文件' },
  { value: 'folder', label: '文件夹' },
  { value: 'other', label: '其他' },
];

// Derived storage-state filter chips .
const STATUS_CHIPS = [
  { value: '', label: '全部状态' },
  { value: 'netdisk', label: '在网盘' },
  { value: 'album', label: '在相册' },
  { value: 'essence', label: '在精华消息' },
  { value: 'none', label: '未下载' },
];

/** Bind a chip row to a store key; the pick callback fires on change. */
function bindChips(container, scopeId, chips, key, onPick) {
  const wrap = container.querySelector(scopeId);
  if (!wrap) return;
  wrap.innerHTML = chips.map((c) =>
    `<button class="type-chip ${c.value === getState()[key] ? 'active' : ''}" data-val="${c.value}">${c.label}</button>`
  ).join('');
  wrap.addEventListener('click', (e) => {
    const btn = e.target.closest('.type-chip');
    if (!btn) return;
    set(key, btn.dataset.val);
    wrap.querySelectorAll('.type-chip').forEach((b) => b.classList.toggle('active', b === btn));
    onPick(btn.dataset.val);
  });
}

/** Files toolbar. Upload entry is merged (local/URL/netdisk/album/essence/
 * browser-text upload all go through the upload modal); refresh has two
 * levels (all list / current group list); "new folder" is root-path only,
 * disabled inside folders (backend flat single-level semantics). */
export function initFilesToolbar(container) {
  container.className = 'toolbar';
  const canNewFolder = !getState().folder;
  container.innerHTML = `
    <span class="btn-group">
      <button id="btn-upload" class="primary" title="上传（本地/URL/网盘/相册/精华/浏览器文本）">${getIcon('UPLOAD', 14)} 上传</button>
      <button id="btn-new-folder" ${canNewFolder ? '' : 'disabled'} title="新建一级文件夹（仅在根路径可用；文件夹内不可再建）">${getIcon('FOLDER', 14)} 新建文件夹</button>
    </span>
    <span class="toolbar-menu">
      <button id="btn-refresh-menu">${getIcon('REFRESH', 13)} 刷新 ${getIcon('CHEVRON_DOWN', 10)}</button>
      <div class="menu-box hidden" id="refresh-menu">
        <button class="menu-item" data-act="scan-all">清空并重新获取全部列表</button>
        <button class="menu-item" data-act="scan-current">清空并重新获取当前群列表</button>
      </div>
    </span>
    <div id="type-chips" class="type-chips"></div>
    <div id="status-chips" class="type-chips"></div>
    <span class="spacer"></span>
    <input id="search-input" type="search" placeholder="搜索完整文件名..." value="${getState().searchQuery || ''}" />
    <input id="file-input" type="file" multiple style="display:none" />
  `;

  bindChips(container, '#type-chips', TYPE_CHIPS, 'fileType', () => {
    set('filePage', 1);
  });
  bindChips(container, '#status-chips', STATUS_CHIPS, 'fileStatus', () => {
    set('filePage', 1);
    refresh('files');
  });

  container.querySelector('#search-input')?.addEventListener('input', debounce(() => {
    set('searchQuery', container.querySelector('#search-input').value);
    set('filePage', 1);
    refresh('files');
  }, 260));

  // Two-tier refresh menu: all list / current group list (no third tier).
  attachMenu(container.querySelector('#btn-refresh-menu'), container.querySelector('#refresh-menu'));
  container.querySelectorAll('#refresh-menu [data-act]').forEach((btn) => {
    btn.addEventListener('click', () => handleRefreshAction(btn.dataset.act));
  });

  container.querySelector('#btn-upload')?.addEventListener('click', showUploadSourceModal);
  container.querySelector('#btn-new-folder')?.addEventListener('click', handleNewFolder);
  container.querySelector('#file-input')?.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
      handleFileUpload(e.target.files);
      e.target.value = '';
    }
  });
}

/** Create a first-level folder (root path only; inside a folder the
 * button is disabled). */
async function handleNewFolder() {
  if (getState().folder) { toast('仅在根路径可新建文件夹', 'warn'); return; }
  const currentGroup = await resolveUploadGroup(getState().currentGroup, 'file');
  if (!currentGroup) { toast('请先选择群', 'warn'); return; }
  const name = await promptEx('新建文件夹', `在群 ${currentGroup} 根路径下新建一级文件夹`, { placeholder: '文件夹名' });
  if (!name) return;
  await mutate('新建', API.FILES.FOLDER_CREATE, { group: currentGroup, name },
    { refresh: 'files', successText: '新建文件夹任务已提交' });
}

async function handleRefreshAction(act) {
  const { currentGroup } = getState();
  if (act === 'scan-current') {
    // Contract: mode=all|range (group_ids); a single group uses range.
    if (!currentGroup) { toast('请先选择群', 'warn'); return; }
    await mutate('刷新', API.FILES.SCAN, { mode: 'range', group_ids: [currentGroup] },
      { successText: '当前群列表刷新已启动' });
  } else {
    // The file-domain "all list" refresh must call files/scan with
    // mode=all (full refetch of all group files); groups/scan only
    // refreshes group info/albums/essence.
    await mutate('刷新', API.FILES.SCAN, { mode: 'all' },
      { successText: '全部列表刷新已启动' });
  }
}

/** Netdisk toolbar: refresh, upload (local relay), URL upload, root,
 * mkdir. */
export function initNetdiskToolbar(container) {
  container.className = 'toolbar';
  container.innerHTML = `
    <span class="btn-group">
      <button id="btn-netdisk-refresh" title="刷新">${getIcon('REFRESH', 13)} 刷新</button>
      <button id="btn-netdisk-upload" class="primary" title="上传（本地 / URL / 群文件 / 相册 / 精华 五来源）">${getIcon('UPLOAD', 13)} 上传</button>
      <button id="btn-netdisk-home" title="根目录">根目录</button>
      <button id="btn-netdisk-mkdir" title="新建目录">新建目录</button>
    </span>
    <div id="netdisk-type-chips" class="type-chips"></div>
  `;

  // Netdisk filters by extension locally (N4a); its chip state is isolated
  // from the files tab so switching tabs never leaks a filter (module rule).
  bindChips(container, '#netdisk-type-chips', NETDISK_CHIPS, 'netdiskType', () => {
    set('netdiskPage', 1);
  });

  container.querySelector('#btn-netdisk-refresh')?.addEventListener('click', () => refresh('netdisk'));
  // Netdisk upload matrix (five sources): local relay / URL link /
  // group files / album / essence.
  container.querySelector('#btn-netdisk-upload')?.addEventListener('click', showNetdiskUploadSourceModal);
  container.querySelector('#btn-netdisk-home')?.addEventListener('click', () => {
    set('netdiskPath', '/');
    set('netdiskPage', 1);
    refresh('netdisk');
  });
  container.querySelector('#btn-netdisk-mkdir')?.addEventListener('click', handleNetdiskMkdir);
}


/** Netdisk upload source modal (local/URL/group files/album/essence),
 * dispatching directly to the matching endpoint. */
async function showNetdiskUploadSourceModal() {
  const res = await showFormModal('上传到网盘：选择来源', [
    { name: 'source', label: '来源', type: 'select', value: 'local', options: [
      { value: 'local', label: '本地文件（两步接力：群文件→转存网盘）' },
      { value: 'url', label: 'URL 链接（离线下载）' },
      { value: 'group', label: '由群文件上传' },
      { value: 'album', label: '由相册上传（图片/视频）' },
      { value: 'essence', label: '由精华上传（文本）' },
    ] },
  ]);
  if (!res?.source) return;
  if (res.source === 'local') return handleNetdiskUploadLocal();
  if (res.source === 'url') return openNetdiskUrlUpload();
  if (res.source === 'group') return openGroupFileToNetdisk();
  if (res.source === 'album') return openAlbumToNetdisk();
  if (res.source === 'essence') return openEssenceToNetdisk();
}

async function handleNetdiskMkdir() {
  const { netdiskPath } = getState();
  const name = await promptEx('新建目录', `在 ${netdiskPath} 下创建目录`, { placeholder: '目录名' });
  if (!name) return;
  const fullPath = netdiskPath === '/' ? `/${name}` : `${netdiskPath}/${name}`;
  await mutate('创建目录', API.BRIDGE.MKDIR, { path: fullPath },
    { refresh: 'netdisk', successText: '目录创建成功' });
}