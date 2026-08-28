/**
 * Stat bar (G5) - aggregate storage stats card.
 *
 * Fetches the stat endpoint for the current context (group or all
 * managed groups when none is focused) and re-renders on group change
 * and file-list refreshes (G4 scheduler).
 *
 * @module components/stat-bar
 */

import { getState, subscribe } from '../store.js';
import { API, apiGet } from '../api.js';
import { formatSize } from '../utils/helpers.js';

/**
 * Initialize the stat strip.
 * @param {HTMLElement} container - <div id="stat-bar">
 */
export function initStatBar(container) {
  container.className = 'stat-bar';

  // 2026-09-03 性能修复（P-2）：refresh:files 高频触发（搜索击键/命令完成/
  // data_changed）→ stat 拉取 400ms 防抖 + 飞行中合并，避免每事件一次 stat 请求。
  let debounceTimer = null;

  async function run() {
    const { currentGroup } = getState();
    try {
      const data = currentGroup
        ? await apiGet(API.STAT, { group: currentGroup })
        : await apiGet(API.STAT);
      container.innerHTML = `
        <span class="stat-item">群: ${data.group_id === '*' ? '全部受管群' : (data.group_id || '-')}</span>
        <span class="stat-item">文件: ${data.file_count ?? 0}</span>
        <span class="stat-item">容量: ${formatSize(data.total_space || data.total_size || 0)}</span>
        ${data.used_space != null ? `<span class="stat-item">已用: ${formatSize(data.used_space)}</span>` : ''}
      `;
    } catch (e) {
      container.innerHTML = '';
    }
  }

  function load() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(run, 400);
  }

  subscribe('currentGroup', load);
  subscribe('refresh:files', load);
  load();
}