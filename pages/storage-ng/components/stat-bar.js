/**
 * Stat bar (G5) - aggregate storage stats card.
 *
 * Fetches the stat endpoint for the current context (group or all
 * managed groups when none is focused) and re-renders on group change
 * and file-list refreshes (G4 scheduler). Operator display (stat
 * `accounts` field): single-group view shows the owning QQ (each file
 * is operated by exactly one account); the global view shows
 * "全部在线账号" instead of listing individual QQs.
 *
 * @module components/stat-bar
 */

import { getState, subscribe, nextSeq, isStale } from '../store.js';
import { API, apiGet } from '../api.js';
import { escapeHtml, formatSize } from '../utils/helpers.js';

/**
 * Initialize the stat strip.
 * @param {HTMLElement} container - <div id="stat-bar">
 */
export function initStatBar(container) {
  container.className = 'stat-bar';

  // refresh:files fires frequently (search keystrokes, command completion,
  // data_changed) -> stat fetches are debounced 400ms and every response is
  // sequence-guarded, so a slow older response can neither win over a newer
  // one nor repaint after a group switch.
  let debounceTimer = null;

  async function run() {
    const { currentGroup } = getState();
    const seq = nextSeq('stat');
    try {
      const data = currentGroup
        ? await apiGet(API.STAT, { group: currentGroup })
        : await apiGet(API.STAT);
      if (isStale('stat', seq)) return; // superseded by a newer request
      // Operator display (stat `accounts`): every single-file operation runs
      // under exactly one account (the group's owning account), so a single
      // group shows that one QQ. The global view aggregates groups across
      // all online accounts — listing individual QQs there would imply
      // multi-account operation of one file, so show the scope instead.
      const operators = (data.accounts || []).filter(Boolean);
      const isGlobal = data.group_id === '*';
      const operatorText = isGlobal
        ? (operators.length ? '全部在线账号' : '-')
        : (operators.length ? operators[0] : '-');
      container.innerHTML = `
        <span class="stat-item">操作者: ${escapeHtml(operatorText)}</span>
        <span class="stat-item">群: ${escapeHtml(data.group_id === '*' ? '全部在线账号所属群' : (data.group_id || '-'))}</span>
        <span class="stat-item">文件: ${data.file_count ?? 0}</span>
        <span class="stat-item">容量: ${formatSize(data.total_space || data.total_size || 0)}</span>
        ${data.used_space != null ? `<span class="stat-item">已用: ${formatSize(data.used_space)}</span>` : ''}
      `;
    } catch (e) {
      if (isStale('stat', seq)) return;
      // 失败必须可见：静默清空会让"统计加载失败"被读成"没有统计数据"。
      container.innerHTML = '<span class="stat-item status-error">'
        + `统计加载失败: ${escapeHtml(String(e && e.message || e || '网络错误'))}</span>`;
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