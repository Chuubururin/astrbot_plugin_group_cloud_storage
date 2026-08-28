/**
 * Folder picker (D8) - lazy directory tree modal for move operations.
 *
 * Group folders are flat (one level), but the picker stays lazy: each
 * folder expands on demand via files/list (folder contract), keeping the
 * initial fetch and the memory footprint minimal.
 *
 * @module features/folder-picker
 */

import { getState } from '../store.js';
import { API, apiGet } from '../api.js';
import { getIcon } from '../icons.js';
import { escapeHtml } from '../utils/helpers.js';

let overlay = null;
let resolvePick = null;
let selectedFolder = { id: '', name: '根目录' };

function ensure() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">选择目标目录</div>
      <div class="modal-body folder-tree" style="max-height:50vh;overflow:auto"></div>
      <div class="modal-actions">
        <button class="modal-cancel">取消</button>
        <button class="modal-ok primary">移动到此处</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-cancel').addEventListener('click', () => done(null));
  overlay.querySelector('.modal-ok').addEventListener('click', () => done(selectedFolder));
  overlay.addEventListener('click', (e) => { if (e.target === overlay) done(null); });
}

function done(value) {
  overlay.classList.add('hidden');
  if (resolvePick) { resolvePick(value); resolvePick = null; }
}

async function fetchFolders(group, folder) {
  const data = await apiGet(API.FILES.LIST, { group, folder, page: 1, page_size: 100 });
  return data.folders || [];
}

function renderNode(container, node, depth, group) {
  const row = document.createElement('div');
  row.className = 'tree-row' + (selectedFolder.id === node.id ? ' active' : '');
  row.style.paddingLeft = `${8 + depth * 16}px`;
  row.innerHTML = `${getIcon('FOLDER', 13)} <span class="tree-label">${escapeHtml(node.name)}</span>`;
  row.addEventListener('click', (e) => {
    if (e.target.closest('.tree-toggle')) return;
    selectedFolder = { id: node.id, name: node.name };
    overlay.querySelectorAll('.tree-row').forEach((r) => r.classList.remove('active'));
    row.classList.add('active');
  });
  container.appendChild(row);

  if (node.id) {
    const childrenBox = document.createElement('div');
    childrenBox.className = 'hidden';
    container.appendChild(childrenBox);
    const toggle = document.createElement('button');
    toggle.className = 'tree-toggle';
    toggle.textContent = '展开';
    row.appendChild(toggle);
    toggle.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (childrenBox.classList.contains('hidden')) {
        if (!childrenBox.dataset.loaded) {
          toggle.textContent = '...';
          const kids = await fetchFolders(group, node.id);
          for (const k of kids) renderNode(childrenBox, k, depth + 1, group);
          childrenBox.dataset.loaded = '1';
        }
        childrenBox.classList.remove('hidden');
        toggle.textContent = '收起';
      } else {
        childrenBox.classList.add('hidden');
        toggle.textContent = '展开';
      }
    });
  }
}

/**
 * Open the folder picker.
 * @param {string} [group] - target group (defaults to the current one)
 * @returns {Promise<{id: string, name: string}|null>} picked folder (null = cancel)
 */
export async function pickFolder(group) {
  ensure();
  const gid = group || getState().currentGroup;
  if (!gid) return null;
  selectedFolder = { id: '', name: '根目录' };
  const tree = overlay.querySelector('.folder-tree');
  tree.innerHTML = '<div class="tree-row active">根目录</div>';
  const roots = await fetchFolders(gid, '');
  for (const f of roots) renderNode(tree, f, 1, gid);
  overlay.classList.remove('hidden');
  return new Promise((r) => { resolvePick = r; });
}