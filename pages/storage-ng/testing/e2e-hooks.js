/**
 * E2E testing hooks (TE-1).
 *
 * With ?e2e=1 in the URL this module:
 *   1. Exposes window.__PAGE_TEST__ (state inspection, mocks, DOM helpers)
 *   2. Replaces the postMessage bridge with a same-origin fetch adapter
 *      (mock mode without auth, or real-machine mode with __E2E_AUTH__),
 *   3. Logs every fetch and refuses emoji in body text.
 *
 * The __PAGE_TEST__ surface is part of the probe contract and must stay
 * stable. Render budget data comes from dom-diff stats only (the former
 * render-budget module was dead code and removed).
 *
 * @module testing/e2e-hooks
 */

import { getState, subscribe, subscribeAll } from '../store.js';

let testApi = null;
const apiLog = [];
const mocks = new Map();

/** Whether E2E mode is enabled via ?e2e=1. */
export function isE2EMode() {
  const params = new URLSearchParams(window.location.search);
  return params.get('e2e') === '1';
}

/** Install the E2E test API and fetch interception. */
export function initE2EHooks() {
  if (!isE2EMode() || testApi) return;
  console.log('[E2E] Test mode enabled');

  // Pre-seeded mocks from the test driver (array of [path, data] pairs).
  if (Array.isArray(window.__E2E_MOCKS__)) {
    for (const [topic, data] of window.__E2E_MOCKS__) mocks.set(topic, data);
  }

  testApi = {
    /** State snapshot with Sets/Maps serialized. */
    getState() {
      return JSON.parse(JSON.stringify(getState(), (_k, v) => {
        if (v instanceof Set) return { __set: [...v] };
        if (v instanceof Map) return { __map: [...v.entries()] };
        return v;
      }));
    },

    /** Keyed-render budget stats . */
    async getRenderStats() {
      const { getDiffStats } = await import('../utils/dom-diff.js');
      return { diff: getDiffStats() };
    },

    /** All intercepted fetch entries. */
    get apiLog() { return [...apiLog]; },

    clearApiLog() { apiLog.length = 0; },

    setMock(path, data) { mocks.set(path, data); },
    getMock(path) { return mocks.get(path); },
    clearMocks() { mocks.clear(); },

    /** Resolve when a store key equals an expected value (or timeout). */
    waitForState(key, expected, timeout = 5000) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          unsub();
          reject(new Error(`Timeout waiting for ${key} = ${expected}`));
        }, timeout);
        const unsub = subscribe(key, (value) => {
          if (value === expected) {
            clearTimeout(timer);
            unsub();
            resolve();
          }
        });
      });
    },

    /** Resolve when a selector appears (MutationObserver fallback). */
    waitForElement(selector, timeout = 5000) {
      return new Promise((resolve, reject) => {
        const el = document.querySelector(selector);
        if (el) { resolve(el); return; }
        const timer = setTimeout(() => {
          observer.disconnect();
          reject(new Error(`Timeout waiting for ${selector}`));
        }, timeout);
        const observer = new MutationObserver(() => {
          const hit = document.querySelector(selector);
          if (hit) {
            clearTimeout(timer);
            observer.disconnect();
            resolve(hit);
          }
        });
        observer.observe(document.body, { childList: true, subtree: true });
      });
    },

    async click(selector) {
      const el = await this.waitForElement(selector);
      el.click();
    },

    async type(selector, value) {
      const el = await this.waitForElement(selector);
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    },

    /** Assert no emoji codepoints anywhere in body text (TE-5). */
    assertNoEmoji() {
      const emojiRegex = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{231A}-\u{23FF}\u{FE0F}]/gu;
      const text = document.body.textContent;
      const hasEmoji = emojiRegex.test(text);
      if (hasEmoji) {
        const hit = text.match(emojiRegex);
        console.error('[E2E] Emoji found in body text!', hit);
      }
      return !hasEmoji;
    },
  };

  window.__PAGE_TEST__ = testApi;
  interceptApiCalls();
  subscribeAll((state) => {
    console.log('[E2E] State changed:', state);
  });
}

/** Intercept window.fetch: log entries and serve mocks by path. */
function interceptApiCalls() {
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const [url, options] = args;
    const entry = {
      timestamp: Date.now(),
      url: typeof url === 'string' ? url : (url && (url.href || url.url)) || String(url),
      method: options?.method || 'GET',
      body: options?.body,
    };
    apiLog.push(entry);

    const path = entry.url
      .replace(/^.*\/api\/v1\/plugins\/extensions\/[^/]+\//, '')
      .split('?')[0];
    if (mocks.has(path)) {
      const mock = mocks.get(path);
      const data = typeof mock === 'function' ? mock(entry) : mock;
      if (data !== undefined) {
        console.log(`[E2E] Mock response for ${path}`);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    try {
      const response = await originalFetch.apply(this, args);
      entry.status = response.status;
      return response;
    } catch (error) {
      entry.error = error.message;
      throw error;
    }
  };
}

/**
 * Replace the postMessage bridge with a same-origin fetch adapter.
 * - mock mode (no __E2E_AUTH__): unauthenticated fetch, SSE off
 * - real-machine mode (__E2E_AUTH__ = dashboard JWT): authenticated
 *   fetch + streaming SSE over the same-origin plugin API
 */
export function disableBridge() {
  if (!isE2EMode()) return;

  const authHeaders = () => {
    const token = window.__E2E_AUTH__;
    return token ? { Authorization: `Bearer ${token}` } : {};
  };
  const apiUrl = (path) =>
    `/api/v1/plugins/extensions/astrbot_plugin_group_cloud_storage/${path}`;

  window.AstrBotPluginPage = {
    ...window.AstrBotPluginPage,

    apiGet(path, params) {
      const url = new URL(apiUrl(path), window.location.origin);
      if (params) {
        Object.entries(params).forEach(([k, v]) => {
          if (v !== undefined) url.searchParams.set(k, v);
        });
      }
      return fetch(url, { headers: authHeaders() }).then((r) => r.json());
    },

    apiPost(path, body) {
      return fetch(apiUrl(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      }).then((r) => r.json());
    },

    upload(path, file) {
      const fd = new FormData();
      fd.append('file', file, file?.name || 'upload.bin');
      return fetch(apiUrl(path), {
        method: 'POST',
        headers: authHeaders(),
        body: fd,
      }).then((r) => r.json());
    },

    download(path, params) {
      const url = new URL(apiUrl(path), window.location.origin);
      if (params) {
        Object.entries(params).forEach(([k, v]) => {
          if (v !== undefined) url.searchParams.set(k, v);
        });
      }
      return fetch(url, { headers: authHeaders() });
    },

    subscribeSSE(path, handlersInput) {
      // The host bridge takes a handlers object ({onMessage, onError});
      // api.js wraps it and passes the object in (defensive: a bare
      // handler function is also tolerated).
      const onMessage = typeof handlersInput === 'function' ? handlersInput : handlersInput?.onMessage;
      const controller = new AbortController();
      const url = new URL(apiUrl(path || 'events'), window.location.origin);
      (async () => {
        try {
          const resp = await fetch(url, {
            headers: { Accept: 'text/event-stream', ...authHeaders() },
            signal: controller.signal,
          });
          if (!resp.ok || !resp.body) throw new Error(`SSE ${resp.status}`);
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) > -1) {
              const block = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              for (const line of block.split('\n')) {
                if (!line.startsWith('data:')) continue;
                try {
                  const parsed = JSON.parse(line.slice(5).trim());
                  if (onMessage) onMessage({ raw: line.slice(5).trim(), parsed, eventType: 'message' });
                } catch (e) { /* non-JSON keepalive */ }
              }
            }
          }
        } catch (e) {
          if (!controller.signal.aborted) {
            console.warn('[E2E] SSE stream ended:', e);
          }
        }
      })();
      return () => controller.abort();
    },

    onContext() {
      return { theme: 'dark', platform: 'web' };
    },
  };
}