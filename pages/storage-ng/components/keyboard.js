/**
 * Global keyboard shortcuts (mainstream control paradigm).
 *
 * @module keyboard
 */

import { getState } from '../store.js';

/**
 * Views with no list selection to act on: groups keeps its selection inside
 * group-table.js, tasks/config have none. sourceFor() falls back to
 * GROUP_SOURCE for these ids, so without this guard Escape/Ctrl+A silently
 * rewrote fileSelected (the files tab's set) from a tab where the user
 * cannot see it.
 */
const NO_LIST_SELECTION_VIEWS = new Set(['groups', 'tasks', 'config']);

export function initKeyboard() {
  document.addEventListener('keydown', async (e) => {
    // Escape 优先属于打开的浮层 (模态/菜单/右键菜单): WAI-ARIA dialog
    // 惯例下它只关闭对话框, 不得连带清空下层列表的选择 ("取消无副作用").
    // 这些浮层各自挂了 keydown 监听, 全局快捷键在此让位.
    if (e.key === 'Escape') {
      const overlayOpen = document.querySelector('.modal-overlay:not(.hidden), .ctx-menu:not(.hidden), .menu-box:not(.hidden)');
      if (overlayOpen) return;
      const view = getState().currentView;
      if (NO_LIST_SELECTION_VIEWS.has(view)) return;
      const { sourceFor } = await import('../features/data-sources.js');
      sourceFor(view).selection.clear();
      return;
    }
    // Ctrl/Cmd+A: select all rows of the active list (not inside inputs).
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      const tag = e.target?.tagName;
      if (tag && /INPUT|TEXTAREA|SELECT/.test(tag)) return;
      // 浮层打开时 Ctrl+A 属于对话框内的文本全选, 不是列表全选: 让位给
      // 浏览器默认行为, 否则会在模态背后改写 fileSelected.
      if (document.querySelector('.modal-overlay:not(.hidden)')) return;
      const view = getState().currentView;
      if (NO_LIST_SELECTION_VIEWS.has(view)) return;
      e.preventDefault();
      const { sourceFor } = await import('../features/data-sources.js');
      const source = sourceFor(view);
      const items = getState()[source.itemsKey] || [];
      source.selection.setMany(items.filter((f) => !f.is_dir).map(source.rowKey));
    }
  });
}
