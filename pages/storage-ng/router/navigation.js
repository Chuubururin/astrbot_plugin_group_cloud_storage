/** Navigation helpers — back, forward, breadcrumbs. */

import { navigate } from './hash.js';

export function goBack() {
  window.history.back();
}

export function goForward() {
  window.history.forward();
}

export function goTo(path) {
  navigate(path);
}

export function getBreadcrumbs(path) {
  return path.split('/').filter(Boolean).map((segment, i, arr) => ({
    label: segment,
    path: '/' + arr.slice(0, i + 1).join('/'),
  }));
}
