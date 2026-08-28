/**
 * Router - view switching with URL hash sync (FE-4).
 *
 * Views are lazy-loaded: main.js registers render functions that return
 * a cleanup, and navigation disposes the previous view before mounting
 * the next one. Legacy hashes (e.g. #view=bridge) are aliased onto the
 * tabs that absorbed their functionality.
 *
 * v2.13: generation counter prevents stale async imports from overwriting
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

/** History aliases: old view ids -> current tab ids (功能只增不减). */
const VIEW_ALIASES = { bridge: 'netdisk' };

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
  window.location.hash = Object.entries(hashParams)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join('&');

  const content = document.getElementById('content');
  if (content) {
    content.innerHTML = '';
    const myGen = ++generation;
    const result = views.get(target)(content, params);
    if (result && typeof result.then === 'function') {
      result.then((cleanup) => {
        if (myGen !== generation) return;
        if (typeof cleanup === 'function') currentCleanup = cleanup;
      }).catch((err) => {
        if (myGen !== generation) return;
        console.error(`[router] view ${target} failed:`, err);
      });
    } else if (typeof result === 'function') {
      currentCleanup = result;
    }
  }
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