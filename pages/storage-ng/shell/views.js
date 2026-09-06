/** View registry — manages view lifecycle and mounting. */

const _views = new Map();
let _currentView = null;

export function registerView(name, factory) {
  _views.set(name, factory);
}

export function getCurrentView() {
  return _currentView;
}

export async function mountView(name, container, props = {}) {
  // Dispose current view
  if (_currentView && _currentView.dispose) {
    _currentView.dispose();
  }
  const factory = _views.get(name);
  if (!factory) {
    console.warn(`[views] unknown view: ${name}`);
    return null;
  }
  const view = await factory(container, props);
  _currentView = view;
  return view;
}

export function disposeCurrentView() {
  if (_currentView && _currentView.dispose) {
    _currentView.dispose();
    _currentView = null;
  }
}
