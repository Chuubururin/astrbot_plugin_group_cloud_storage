/**
 * Netdisk local upload - two-step relay (W3-D).
 *
 * There is no direct OpenList upload endpoint in this plugin, so the
 * flow honestly relays through existing capabilities (zero new endpoints):
 *
 *   local file -> group files (prepare/upload, recommended group)
 *              -> wait terminal state via the task ledger ('tasks')
 *              -> locate the resource by name (exact match)
 *              -> bridge/transfer to the netdisk root
 *
 * The group-file intermediate copy is KEPT (never auto-deleted; every UI
 * copy states this explicitly). The core is dependency-injected so the
 * unit tests can drive it with stub apiPost/apiGet/upload.
 *
 * @module features/netdisk-upload
 */

import { getState, refresh } from '../store.js';
import { API } from '../api.js';
import { confirmEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';

const POLL_MS = 1500;               // task-ledger poll interval
const TIMEOUT_MS = 240000;          // relay timeout (4 min)

/** UI entry: confirm the relay semantics, pick files, run the relay. */
export async function handleNetdiskUploadLocal() {
  const ok = await confirmEx('本地上传到网盘',
    '流程：本地文件 → 上传到群文件（推荐群）→ 自动转存网盘根目录。' +
    '群文件中间副本将保留（可自行删除）。确定继续？',
    { okText: '选择文件' });
  if (!ok) return;
  const conv = await showFormModal('格式转换', [
    { name: 'convert_to', label: '目标格式', type: 'select', value: '', options: [
      { value: '', label: '保持原格式' },
      { value: 'mp4', label: '转为 MP4（视频）' },
      { value: 'mkv', label: '转为 MKV（视频）' },
      { value: 'webm', label: '转为 WebM（视频）' },
      { value: 'png', label: '转为 PNG（图片）' },
      { value: 'jpg', label: '转为 JPG（图片）' },
      { value: 'webp', label: '转为 WebP（图片）' },
    ] },
  ]);
  if (!conv) return;
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.onchange = async () => {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    try {
      const st = await uploadFilesToNetdisk(files, {
        apiPost: (p, b) => window.AstrBotPluginPage.apiPost(p, b),
        apiGet: (p, q) => window.AstrBotPluginPage.apiGet(p, q),
        upload: (p, f) => window.AstrBotPluginPage.upload(p, f, undefined),
        group: getState().currentGroup || '',
      }, { convertTo: conv.convert_to || '' });
      report(st);
      refresh('netdisk');
      refresh('bridge');
      refresh('files');
      refresh('tasks');
    } catch (e) {
      toast(`网盘上传失败: ${e.message || ''}`, 'error');
    }
  };
  input.click();
}

/**
 * Two-step relay core (unit-testable).
 * @param {File[]} files
 * @param {{apiPost: Function, apiGet: Function, upload: Function, group: string}} deps
 * @returns {Promise<{total: number, uploaded: number, transferred: number, failed: string[]}>}
 */
export async function uploadFilesToNetdisk(files, deps, options = {}) {
  const total = files.length;
  const convertTo = options.convertTo || '';
  let group = deps.group;
  // N-07 default rule: smallest group id with enough free space (best-effort).
  if (!group) {
    const size = files.reduce((s, f) => s + (f.size || 0), 0);
    try {
      // 2026-09-03 修复：recommend-group 为 GET 端点（apiGet；此前 POST→405 缺省失败）
      const rec = await deps.apiGet(API.FILES.RECOMMEND_GROUP, { kind: 'file', size });
      group = (rec && rec.recommended && rec.recommended.group_id) || '';
    } catch (e) { /* best-effort */ }
  }
  if (!group) throw new Error('无可用目标群（请先在群组 Tab 加载并选择群）');

  // 1) Upload jobs (two-phase); track names + task ids for the wait step.
  const jobs = [];
  const failed = [];
  for (const f of files) {
    try {
      const isVideo = /\.(mp4|mkv|avi|mov|flv|webm|wmv)$/i.test(f.name);
      const isImage = /\.(png|jpe?g|webp|bmp|gif)$/i.test(f.name);
      const convertOk = (isVideo && ['mp4', 'mkv', 'webm'].includes(convertTo))
        || (isImage && ['png', 'jpg', 'jpeg', 'webp'].includes(convertTo));
      const outName = convertOk ? `${f.name.replace(/\.[^.]+$/, '')}.${convertTo}` : f.name;
      const prep = await deps.apiPost(API.FILES.UPLOAD_PREPARE, {
        group, name: outName, size: f.size, convert_to: convertOk ? convertTo : undefined,
      });
      if (!prep?.token) throw new Error('prepare 未返回 token');
      const up = await deps.upload(`${API.FILES.UPLOAD}/${prep.token}`, f);
      jobs.push({ name: outName, taskId: up?.task_id || '' });
    } catch (e) {
      failed.push(`${f.name}（上传提交失败）`);
    }
  }

  // 2) Wait for terminal states on the task ledger; timeouts surface as failures.
  const done = await waitTasksDone(jobs.map((j) => j.taskId).filter(Boolean), { ...deps, group });
  for (const j of jobs) {
    if (!j.taskId || done.get(j.taskId) !== 'done') {
      if (!failed.includes(j.name)) failed.push(`${j.name}（上传未完成）`);
    }
  }

  // 3) Locate successful uploads by exact name, then bridge them out.
  const okJobs = jobs.filter((j) => done.get(j.taskId) === 'done');
  const transferred = [];
  for (const j of okJobs) {
    const rid = await locateById(deps, group, j.name);
    if (!rid) { failed.push(`${j.name}（转存前定位失败）`); continue; }
    try {
      await deps.apiPost(API.BRIDGE.TRANSFER, { resource_ids: [rid], group });
      transferred.push(j.name);
    } catch (e) {
      failed.push(`${j.name}（转存提交失败）`);
    }
  }

  return { total, uploaded: jobs.length, transferred: transferred.length, failed };
}

/**
 * Poll the task ledger until every task reaches a terminal state or the
 * deadline passes. Returns a map task_id -> terminal state.
 * @param {string[]} taskIds
 * @param {Object} deps - {apiPost, group}
 * @param {number} [timeoutMs]
 * @returns {Promise<Map<string, string>>}
 */
export async function waitTasksDone(taskIds, deps, timeoutMs = TIMEOUT_MS) {
  const result = new Map();
  const pending = new Set(taskIds);
  const deadline = Date.now() + timeoutMs;
  while (pending.size > 0 && Date.now() < deadline) {
    try {
      const r = await deps.apiPost(API.TASKS, { target: deps.group, limit: 100 });
      for (const t of r?.tasks || []) {
        if (pending.has(t.task_id) && (t.state === 'done' || t.state === 'failed')) {
          result.set(t.task_id, t.state);
          pending.delete(t.task_id);
        }
      }
    } catch (e) { /* transient poll failure: keep retrying */ }
    if (pending.size > 0) await sleep(POLL_MS);
  }
  return result;
}

/** Exact-name lookup: newest uploads sort first (created_at desc default). */
async function locateById(deps, group, name) {
  try {
    const r = await deps.apiGet(API.FILES.LIST, { group, q: name, page: 1, page_size: 20 });
    const hit = (r.items || []).find((it) => it.name === name && !it.is_dir);
    return hit ? Number(hit.id) : 0;
  } catch (e) {
    return 0;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function report(st) {
  const parts = [];
  if (st.uploaded > 0) parts.push(`${st.uploaded}/${st.total} 上传提交`);
  if (st.transferred > 0) parts.push(`${st.transferred} 个已转存网盘`);
  if (st.failed.length > 0) parts.push(`失败 ${st.failed.length}: ${st.failed.join('、')}`);
  toast(`网盘上传（本地接力）完成：${parts.join('；') || '无'}。群文件中间副本保留`,
    st.failed.length ? 'warn' : 'success');
}