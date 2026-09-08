/**
 * Router - view switching with URL hash sync .
 *
 * Views are lazy-loaded: main.js registers render functions that return
 * a cleanup, and navigation disposes the previous view before mounting
 * the next one. Legacy hashes (e.g. #view=bridge) are aliased onto the
 * tabs that absorbed their functionality.
 *
 * A generation counter prevents stale async imports from overwriting
 * currentCleanup when tabs are switched rapidly.
 *
 * @module router
 */

import { getState, set } from './store.js';

/** @type {Map<string, function>} view name -> async render(container, params) */
const views = new Map();

/** @type {function|null} dispose callback of the mounted view */
let currentCleanup = null;

/** Generation counter: incremented on each navigate(), stale imports are discarded. */
let generation = 0;

/** History aliases: old view ids -> current tab ids (old links keep working). */
const VIEW_ALIASES = { bridge: 'netdisk' };

/** Max retries for dynamic import failures (exponential backoff). */
const MAX_RETRIES = 3;
const RETRY_BASE_MS = 300;

/**
 * sessionStorage guard backed by an in-memory map. The dashboard embeds this
 * page in a sandboxed iframe without allow-same-origin, where the
 * sessionStorage getter itself throws SecurityError (opaque origin) — every
 * access must be guarded, reads included.
 */
const memoryGuard = new Map();

function readReloadGuard(key) {
  try {
    return Number(sessionStorage.getItem(key) || 0);
  } catch (e) {
    return memoryGuard.get(key) || 0;
  }
}

function writeReloadGuard(key, value) {
  try {
    sessionStorage.setItem(key, String(value));
  } catch (e) {
    memoryGuard.set(key, value);
  }
}

/** Register a view renderer. */
export function registerView(name, render) {
  views.set(name, render);
}

function resolve(name) {
  return VIEW_ALIASES[name] || name;
}

function parseHash() {
  const params = {};
  const hash = window.location.hash.slice(1);
  if (!hash) return params;
  for (const pair of hash.split('&')) {
    const [k, v] = pair.split('=').map(decodeURIComponent);
    params[k] = v;
  }
  return params;
}

/**
 * Navigate to a view; disposes the previous view and updates the hash.
 * @param {string} name - view id
 * @param {Object} [params] - extra hash params
 */
export function navigate(name, params = {}) {
  const target = resolve(name);
  if (!views.has(target)) {
    console.warn(`[router] unknown view: ${target}`);
    return;
  }
  if (currentCleanup) {
    try { currentCleanup(); } catch (e) { console.error('[router] cleanup error:', e); }
    currentCleanup = null;
  }
  set('currentView', target);

  const hashParams = { view: target, ...params };
  // Hash assignment is one of the few navigation channels a sandboxed iframe
  // (no allow-same-origin) still permits; guard it anyway so a restriction
  // can never abort the view mount below.
  try {
    window.location.hash = Object.entries(hashParams)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
      .join('&');
  } catch (e) { /* hash navigation unavailable in this embedding */ }

  const content = document.getElementById('content');
  if (!content) return;

  content.innerHTML = '';
  const myGen = ++generation;
  // Attach generation to container so view renderers can detect staleness
  // after a slow dynamic import resolves.
  content.dataset.routerGen = myGen;

  /** Try to mount the view; on failure retry with exponential backoff. */
  function attemptViewLoad(retryCount) {
    const currentGen = generation;
    try {
      const result = views.get(target)(content, params);
      if (result && typeof result.then === 'function') {
        result.then((cleanup) => {
          if (currentGen !== generation) return;
          if (typeof cleanup === 'function') currentCleanup = cleanup;
        }).catch((err) => {
          if (currentGen !== generation) return;
          console.error(`[router] view ${target} load error (attempt ${retryCount + 1}/${MAX_RETRIES + 1}):`, err);
          if (retryCount < MAX_RETRIES) {
            const delay = RETRY_BASE_MS * Math.pow(2, retryCount);
            setTimeout(() => {
              if (currentGen !== generation) return;
              content.innerHTML = '';
              attemptViewLoad(retryCount + 1);
            }, delay);
          } else {
            reloadAfterImportPoison(target);
          }
        });
      } else if (typeof result === 'function') {
        currentCleanup = result;
      }
    } catch (err) {
      console.error(`[router] view ${target} sync error (attempt ${retryCount + 1}/${MAX_RETRIES + 1}):`, err);
      if (retryCount < MAX_RETRIES) {
        const delay = RETRY_BASE_MS * Math.pow(2, retryCount);
        setTimeout(() => {
          if (currentGen !== generation) return;
          content.innerHTML = '';
          attemptViewLoad(retryCount + 1);
        }, delay);
      } else {
        reloadAfterImportPoison(target);
      }
    }
  }

  /**
   * Last-resort recovery when every in-page retry failed: any module of the
   * view's import subgraph that failed mid-flight stays poisoned in the
   * browser module map for the document's lifetime (only the top-level
   * specifier can be cache-busted; static sub-imports keep their URL), so
   * clicking the tab again can never recover. One automatic reload refetches
   * the whole module graph; the sessionStorage guard prevents a reload loop
   * if the server is genuinely down (hint stays visible for that case).
   */
  function reloadAfterImportPoison(target) {
    content.innerHTML = '<div class="empty-hint" style="padding:24px">'
      + '视图加载失败，正在自动恢复…</div>';
    const guardKey = 'view-load-reload:' + target;
    const last = readReloadGuard(guardKey);
    if (Date.now() - last < 15000) {
      content.innerHTML = '<div class="empty-hint" style="padding:24px">'
        + '视图加载失败，请点击顶部标签重试</div>';
      return;
    }
    writeReloadGuard(guardKey, Date.now());
    console.warn(`[router] view ${target}: import graph poisoned, reloading page to recover`);
    try {
      window.location.reload();
    } catch (e) {
      // Reload navigation can be refused in restrictive embeds; leave the
      // manual-retry hint so the user still has a recovery path.
      console.error('[router] reload refused:', e);
      content.innerHTML = '<div class="empty-hint" style="padding:24px">'
        + '视图加载失败，请点击顶部标签重试</div>';
    }
  }

  attemptViewLoad(0);
}

/** Current view id. */
export function getCurrentView() {
  return getState().currentView;
}

/** Initialize: bind hashchange and mount the initial route. */
export function initRouter() {
  window.addEventListener('hashchange', () => {
    const params = parseHash();
    const view = resolve(params.view || 'files');
    if (view !== getState().currentView) {
      navigate(view, params);
    }
  });
  const params = parseHash();
  navigate(resolve(params.view || 'files'), params);
}