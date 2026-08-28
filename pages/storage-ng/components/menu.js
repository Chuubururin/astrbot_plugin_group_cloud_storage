/**
 * Menu - unified dropdown menu (one component for every menu button).
 *
 * Click outside or Escape closes any open menu; opening one closes the
 * others. Sandbox-safe: plain DOM, no native popup APIs.
 *
 * @module components/menu
 */

const openMenus = new Set();
let globalHandlersInstalled = false;

function ensureGlobal() {
  if (globalHandlersInstalled) return;
  globalHandlersInstalled = true;
  document.addEventListener('click', () => {
    for (const m of openMenus) m.classList.add('hidden');
    openMenus.clear();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    for (const m of openMenus) m.classList.add('hidden');
    openMenus.clear();
  });
}

/**
 * Attach a dropdown menu to a trigger button.
 * @param {HTMLElement} btn - trigger
 * @param {HTMLElement} menu - .menu-box element (items carry data-act)
 * @returns {function} detach
 */
export function attachMenu(btn, menu) {
  ensureGlobal();
  if (!btn || !menu) return () => {};
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = !menu.classList.contains('hidden');
    for (const m of openMenus) m.classList.add('hidden');
    openMenus.clear();
    if (!isOpen) {
      menu.classList.remove('hidden');
      openMenus.add(menu);
    }
  });
  menu.querySelectorAll('[data-act]').forEach((item) => {
    item.addEventListener('click', () => {
      menu.classList.add('hidden');
      openMenus.delete(menu);
    });
  });
  return () => { openMenus.delete(menu); };
}