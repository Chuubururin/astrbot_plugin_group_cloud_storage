/**
 * Group open-state gate - the shared precheck for every group-scoped
 * entry point (file list / stats / albums / essence / upload).
 *
 * Doc: reading or writing a group runs three checks - the group is
 * managed, its owning account is online, and the group is not dissolved.
 * The backend answers `groups/open-state` with HTTP 200 and puts the
 * verdict in the body (`{managed, account_online, seen, reason}`), with
 * `reason: null` on success. Unlike the other group endpoints this gate is
 * NOT a 403, so call sites read the body instead of catching a status.
 *
 * The reasons are machine values; the dictionary below is the only place
 * they become user-facing text, so a raw English machine value is never
 * surfaced to the user.
 *
 * Extracted from features/group-data.js so that module stays within the
 * <=300-line budget.
 *
 * @module features/group-open-state
 */

import { set } from '../store.js';
import { API, apiGet } from '../api.js';

/** open-state machine reasons -> user-facing labels. */
export const OPEN_STATE_REASONS = {
  'group not managed': '该群未受管理，暂不可操作',
  '群归属账号离线，暂不可操作': '群归属账号离线，暂不可操作',
  '群已解散或不可访问': '群已解散或不可访问',
};

/**
 * Localize an open-state reason. A value that is already user-facing text
 * passes through; an unmapped machine value degrades to a generic label
 * rather than leaking to a toast.
 * @param {string} reason
 * @returns {string}
 */
export function openStateReasonLabel(reason) {
  if (!reason) return '';
  const key = String(reason);
  if (OPEN_STATE_REASONS[key]) return OPEN_STATE_REASONS[key];
  return /[\u4e00-\u9fff]/.test(key) ? key : '该群暂不可操作';
}

/**
 * Precheck one group before it becomes the page's group context.
 * @param {string} groupId
 * @returns {Promise<{ok: boolean, transportError: boolean, reason: string|null,
 *   label: string}>} ok=false carries the localized `label` to show; the raw
 *   machine `reason` stays available for callers that branch on it.
 *   transportError=true means the verdict is unknown (timeout / network), so
 *   callers must not treat it as a refusal and must not drop the group focus.
 */
export async function checkGroupOpenable(groupId) {
  try {
    const st = await apiGet(API.GROUPS.OPEN_STATE, { group: groupId });
    if (st && st.reason) {
      // 后端给出了明确的拒绝理由：调用方可以安全地清掉群上下文。
      return { ok: false, transportError: false, reason: st.reason, label: openStateReasonLabel(st.reason) };
    }
    return { ok: true, transportError: false, reason: null, label: '' };
  } catch (e) {
    // 超时/网络抖动不是「被拒绝」：结论未知，提示可重试而不是静默改上下文。
    return { ok: false, transportError: true, reason: null,
      label: `群状态校验失败（可重试）: ${(e && e.message) || '网络错误'}` };
  }
}

/**
 * Reset every group-scoped context back to the all-groups aggregate view.
 * Doc: a refused gate falls back to the aggregate instead of keeping the
 * stale (or partially applied) group context.
 */
export function clearGroupFocus() {
  set('currentGroup', '');
  set('folder', '');
  set('folderChain', []);
  set('albumGroup', '');
  set('essenceGroup', '');
  set('albumPage', 1);
  set('essencePage', 1);
}
