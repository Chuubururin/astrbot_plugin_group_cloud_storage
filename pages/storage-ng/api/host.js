/** API host detection — resolves backend URL for SSE and direct requests. */

export function getHost() {
  return window.location.origin;
}

export function getSSEUrl(path) {
  return `${getHost()}${path}`;
}
