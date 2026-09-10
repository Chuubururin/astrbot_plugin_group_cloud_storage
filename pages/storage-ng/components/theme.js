/**
 * Theme following: host AstrBot theme first, system preference fallback,
 * reacting to live system changes.
 *
 * @module theme
 */

import { getContext } from '../api.js';

export function initTheme() {
  const ctx = getContext();
  const hostTheme = ctx && (ctx.theme || '').toLowerCase();

  const apply = () => {
    let theme = 'dark';
    if (hostTheme === 'light' || hostTheme === 'dark') {
      theme = hostTheme;
    } else {
      theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light' : 'dark';
    }
    document.documentElement.setAttribute('data-theme', theme);
  };

  apply();
  try {
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
        if (hostTheme !== 'light' && hostTheme !== 'dark') apply();
      });
    }
  } catch (e) { /* matchMedia unsupported */ }
}
