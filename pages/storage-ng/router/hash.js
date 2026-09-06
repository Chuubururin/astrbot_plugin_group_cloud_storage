/** Hash-based router — monitors hash changes. */

let _onHashChange = null;

export function initHashRouter(callback) {
  _onHashChange = callback;
  window.addEventListener('hashchange', _handleHashChange);
  // Initial route
  _handleHashChange();
}

export function destroyHashRouter() {
  window.removeEventListener('hashchange', _handleHashChange);
  _onHashChange = null;
}

function _handleHashChange() {
  const hash = window.location.hash.slice(1) || '/';
  if (_onHashChange) _onHashChange(hash);
}

export function navigate(path) {
  window.location.hash = path;
}

export function getCurrentPath() {
  return window.location.hash.slice(1) || '/';
}
