/**
 * Tabs - the seven top-level tabs (D-2, ADR-0007 T-0).
 *
 * Order is fixed by the owner spec: 【文件】【相册】【精华】【网盘】
 * 【任务】【群组】【配置】. The active tab mirrors router state.
 *
 * @module components/tabs
 */

import { getState, subscribe } from '../store.js';
import { navigate } from '../router.js';
import { getIcon } from '../icons.js';

const TABS = [
  { id: 'files', label: '文件', icon: 'FILES' },
  { id: 'albums', label: '相册', icon: 'IMAGE' },
  { id: 'essence', label: '精华', icon: 'TEXT' },
  { id: 'netdisk', label: '网盘', icon: 'CLOUD' },
  { id: 'tasks', label: '任务', icon: 'TASKS' },
  { id: 'groups', label: '群组', icon: 'GROUP' },
  { id: 'config', label: '配置', icon: 'SETTINGS' },
];

/**
 * Initialize the tab strip.
 * @param {HTMLElement} container - <nav id="tabs">
 */
export function initTabs(container) {
  container.innerHTML = TABS.map((tab) => `
    <button class="tab-btn ${tab.id === getState().currentView ? 'active' : ''}" data-view="${tab.id}">
      ${getIcon(tab.icon, 14)}<span>${tab.label}</span>
    </button>
  `).join('');

  container.addEventListener('click', (e) => {
    const btn = e.target.closest('.tab-btn');
    if (btn) navigate(btn.dataset.view);
  });

  subscribe('currentView', (view) => {
    container.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });
  });
}