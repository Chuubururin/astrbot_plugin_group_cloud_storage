/** Keyboard shortcuts — global hotkeys. */

const _handlers = new Map();

export function registerShortcut(key, handler, description = '') {
  _handlers.set(key, { handler, description });
}

export function unregisterShortcut(key) {
  _handlers.delete(key);
}

export function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const key = [];
    if (e.ctrlKey || e.metaKey) key.push('ctrl');
    if (e.shiftKey) key.push('shift');
    if (e.altKey) key.push('alt');
    key.push(e.key.toLowerCase());
    const combo = key.join('+');
    const entry = _handlers.get(combo);
    if (entry) {
      e.preventDefault();
      entry.handler(e);
    }
  });
}
