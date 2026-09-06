/** SSE connection manager — Server-Sent Events for real-time updates. */

let _source = null;
let _listeners = new Map();

export function connectSSE(url, onMessage, onError) {
  if (_source) {
    _source.close();
  }
  _source = new EventSource(url);
  _source.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'heartbeat') return;
      onMessage(data);
      // Notify registered listeners
      const fns = _listeners.get(data.type) || [];
      fns.forEach((fn) => fn(data));
    } catch (err) {
      console.warn('[SSE] parse error:', err);
    }
  };
  _source.onerror = (e) => {
    console.warn('[SSE] connection error:', e);
    if (onError) onError(e);
  };
  return _source;
}

export function disconnectSSE() {
  if (_source) {
    _source.close();
    _source = null;
  }
}

export function onSSEEvent(type, fn) {
  if (!_listeners.has(type)) {
    _listeners.set(type, []);
  }
  _listeners.get(type).push(fn);
  return () => {
    const arr = _listeners.get(type) || [];
    const idx = arr.indexOf(fn);
    if (idx >= 0) arr.splice(idx, 1);
  };
}
