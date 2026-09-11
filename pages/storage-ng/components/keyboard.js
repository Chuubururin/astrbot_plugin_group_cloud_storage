/**
 * Global keyboard shortcuts (mainstream control paradigm).
 *
 * @module keyboard
 */

import { getState } from '../store.js';

export function initKeyboard() {
  document.addEventListener('keydown', async (e) => {
    // Escape 优先属于打开的浮层 (模态/菜单/右键菜单): WAI-ARIA dialog
    // 惯例下它只关闭对话框, 不得连带清空下层列表的选择 ("取消无副作用").
    // 这些浮层各自挂了 keydown 监听, 全局快捷键在此让位.
    if (e.key === 'Escape') {
      const overlayOpen = document.querySelector('.modal-overlay:not(.hidden), .ctx-menu:not(.hidden), .menu-box:not(.hidden)');
      if (overlayOpen) return;
      const { sourceFor } = await import('./data-sources.js');
      sourceFor(getState().currentView).selection.clear();
      return;
    }
    // Ctrl/Cmd+A: select all rows of the active list (not inside inputs).
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      const tag = e.target?.tagName;
      if (tag && /INPUT|TEXTAREA|SELECT/.test(tag)) return;
      e.preventDefault();
      const { sourceFor } = await import('./data-sources.js');
      const source = sourceFor(getState().currentView);
      const items = getState()[source.itemsKey] || [];
      source.selection.setMany(items.filter((f) => !f.is_dir).map(source.rowKey));
    }
  });
}
