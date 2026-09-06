/** Global error handler — catches unhandled errors and displays toast. */

let _toastFn = null;

export function setErrorToast(fn) {
  _toastFn = fn;
}

export function handleError(err, context = '') {
  const msg = err?.message || String(err);
  console.error(`[storage-ng] ${context}:`, err);
  if (_toastFn) {
    _toastFn(`Error: ${msg}`, 'error');
  }
}

export function initErrorHandling() {
  window.addEventListener('error', (e) => {
    handleError(e.error, 'unhandled');
  });
  window.addEventListener('unhandledrejection', (e) => {
    handleError(e.reason, 'unhandled rejection');
  });
}
