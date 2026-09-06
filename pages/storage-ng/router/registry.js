/** Router registry — view name to factory mapping. */

const _routes = new Map();

export function defineRoute(path, viewName) {
  _routes.set(path, viewName);
}

export function resolveRoute(path) {
  return _routes.get(path) || null;
}

export function getRoutes() {
  return new Map(_routes);
}
