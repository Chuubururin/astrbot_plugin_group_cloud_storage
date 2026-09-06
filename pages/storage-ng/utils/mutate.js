/**
 * Mutation flow helper - the standard write lifecycle for direct
 * (non-command-layer) call sites: POST -> success toast -> refresh,
 * with one unified failure toast that carries the server error message.
 *
 * The command layer (features/commands.js runCommand) stays the
 * lifecycle owner for user-initiated commands; this helper covers the
 * straightforward one-POST handlers outside it so they neither
 * hand-roll try/catch + toast + refresh nor skip error handling.
 * Multi-step pipelines (upload relays, pollers) keep their own flow.
 *
 * @module utils/mutate
 */

import { apiPost } from '../api.js';
import { refresh } from '../store.js';
import { toast } from '../components/toast.js';
import { refetchRows } from './recover.js';

/**
 * @param {string} label - operation name (failure toast: `${label}失败: ...`)
 * @param {string} path - API path
 * @param {any} [body] - POST body
 * @param {Object} [opts]
 * @param {string|string[]} [opts.refresh] - store topic(s) refreshed on success
 * @param {string} [opts.successText] - success toast text (default `${label}成功`)
 * @param {boolean} [opts.toastSuccess=true] - emit the success toast
 * @param {{sourceId: string, rows: Object[]}} [opts.recoverRows] - on
 *   failure, re-pull these rows' authoritative info from cloud storage
 * @returns {Promise<{ok: boolean, data: any}>} ok=false after the failure toast
 */
export async function mutate(label, path, body, opts = {}) {
  try {
    const data = await apiPost(path, body);
    if (opts.refresh) {
      for (const t of Array.isArray(opts.refresh) ? opts.refresh : [opts.refresh]) refresh(t);
    }
    if (opts.toastSuccess !== false) toast(opts.successText || `${label}成功`, 'success');
    return { ok: true, data };
  } catch (e) {
    toast(`${label}失败: ${(e && e.message) || e}`, 'error');
    if (opts.recoverRows) {
      try {
        const recovered = await refetchRows(
          opts.recoverRows.sourceId, opts.recoverRows.rows);
        if (recovered) toast('已从云端重新拉取该条目信息', 'info');
      } catch {
        // Best-effort recovery; never mask the original failure.
      }
    }
    return { ok: false, data: null };
  }
}
