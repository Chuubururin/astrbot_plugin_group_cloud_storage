/** API transport — wraps AstrBot postMessage bridge SDK. */

let _bridge = null;

export function setBridge(bridge) {
  _bridge = bridge;
}

export function getBridge() {
  return _bridge;
}

export async function apiGet(path, params = {}) {
  if (!_bridge) throw new Error('bridge not initialized');
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  });
  return _bridge.get(url.pathname + url.search);
}

export async function apiPost(path, body = null) {
  if (!_bridge) throw new Error('bridge not initialized');
  return _bridge.post(path, body);
}
