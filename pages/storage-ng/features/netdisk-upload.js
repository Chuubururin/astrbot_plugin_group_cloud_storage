/**
 * Netdisk local upload - two-step relay .
 *
 * There is no direct OpenList upload endpoint in this plugin, so the
 * flow honestly relays through existing capabilities (zero new endpoints):
 *
 *   local file -> group files (prepare/upload, recommended group)
 *              -> wait terminal state via the task ledger ('tasks')
 *              -> locate the uploaded resource (task ledger id first,
 *                 then exact-name lookup excluding pre-existing ids)
 *              -> bridge/transfer to the netdisk root
 *
 * The group-file intermediate copy is KEPT (never auto-deleted; every UI
 * copy states this explicitly). The core is dependency-injected so the
 * unit tests can drive it with stub apiPost/apiGet/upload.
 *
 * @module features/netdisk-upload
 */

import { getState, refresh } from '../store.js';
import { API, apiGet, apiPost, upload as bridgeUpload } from '../api.js';
import { convertTargetName, uploadOnce } from './upload.js';
import { confirmEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import {
  convertAllowed, convertOptionsFor, kindsOf, mediaKind,
} from './ingest-options.js';
import { MAX_PAGE_SIZE } from '../constants.js';

const POLL_MS = 1500;               // task-ledger poll interval
const BASE_TIMEOUT_MS = 240000;     // relay timeout floor (4 min)
const TIMEOUT_PER_MB_MS = 800;      // extra budget per MB (volume uploads)

/** Timeout scales with total bytes: the backend force-volumes files above
 * 95MB, and a multi-part upload of 1GB takes minutes more than a 4-minute
 * fixed window. Floor stays 4min for tiny files. */
function relayTimeoutMs(files) {
  const totalMb = files.reduce((s, f) => s + (f.size || 0), 0) / (1024 * 1024);
  return BASE_TIMEOUT_MS + Math.ceil(totalMb) * TIMEOUT_PER_MB_MS;
}

/** Ledger terminal states (no further transition): success, failure and
 * user-intervened cancellation. paused/retry stay non-terminal -- the task
 * will still run again. Aligned with the Celery READY-states convention
 * (SUCCESS/FAILURE/REVOKED): a revoked (cancelled) task never completes,
 * so waiting on it would stall the relay for the full timeout. */
const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled']);

/** UI entry: confirm the relay semantics, pick files, choose the format
 * (asked AFTER the files are known), run the relay. */
export async function handleNetdiskUploadLocal() {
  const ok = await confirmEx('本地上传到网盘',
    '流程：本地文件 → 上传到群文件（推荐群）→ 自动转存网盘根目录。' +
    '群文件中间副本将保留（可自行删除）。确定继续？',
    { okText: '选择文件' });
  if (!ok) return;
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.onchange = async () => {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    // 格式转换必须在拿到文件之后再问：先问再选文件时用户无从得知媒体类型，
    // 选了与类型不符的目标只会被静默丢弃（既不提示也不改名）。
    const options = convertOptionsFor(kindsOf(files));
    let convertTo = '';
    if (options.length) {
      const conv = await showFormModal('格式转换', [
        { name: 'convert_to', label: '目标格式（仅对类型匹配的文件生效）', type: 'select', value: '', options },
      ]);
      if (!conv) return;
      convertTo = conv.convert_to || '';
      if (convertTo) {
        const mismatch = files.filter((f) => !convertAllowed(mediaKind(f.name), convertTo)).length;
        if (mismatch) toast(`${mismatch} 个文件的类型与所选格式不符，将保持原格式`, 'warn');
      }
    }
    try {
      // 传输层统一走 api.js：那里的 API_TIMEOUT 竞速包络是唯一的超时保护，
      // 直连 window.AstrBotPluginPage 会让 waitTasksDone 每轮都裸奔。
      const st = await uploadFilesToNetdisk(files, {
        apiPost, apiGet, upload: bridgeUpload,
        group: getState().currentGroup || '',
      }, { convertTo });
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
 * @param {{convertTo?: string}} [options]
 * @returns {Promise<{total: number, uploaded: number, transferred: number, failed: string[]}>}
 */
export async function uploadFilesToNetdisk(files, deps, options = {}) {
  const total = files.length;
  const convertTo = options.convertTo || '';
  let group = deps.group;
  // Default rule: smallest group id with enough free space (best-effort).
  if (!group) {
    const size = files.reduce((s, f) => s + (f.size || 0), 0);
    try {
      // recommend-group is a GET endpoint (apiGet).
      const rec = await deps.apiGet(API.FILES.RECOMMEND_GROUP, { kind: 'file', size });
      group = (rec && rec.recommended && rec.recommended.group_id) || '';
    } catch (e) { /* best-effort */ }
  }
  if (!group) throw new Error('无可用目标群（请先在群组 Tab 加载并选择群）');

  // Ids already present before the relay: a same-named file that existed
  // beforehand must never be mistaken for the fresh upload. null = the
  // listing was truncated, so which same-named id is new cannot be known.
  const preexisting = await snapshotIds(deps, group);

  // 1) Upload jobs (two-phase); track names + task ids for the wait step.
  const jobs = [];
  const failed = [];
  for (const f of files) {
    const convertOk = convertAllowed(mediaKind(f.name), convertTo);
    const outName = convertTargetName(f.name, convertOk ? convertTo : '');
    try {
      const r = await uploadOnce(group, { file: f, name: outName }, {
        convert_to: convertOk ? convertTo : undefined,
      }, deps);
      if (!r.ok) throw new Error(r.error || '上传提交失败');
      jobs.push({ name: outName, taskId: r.result?.task_id || '' });
    } catch (e) {
      failed.push(`${outName}（上传提交失败: ${(e && e.message) || e}）`);
    }
  }

  // 2) Wait for terminal states on the task ledger; timeouts surface as failures.
  const rids = new Map();
  const done = await waitTasksDone(
    jobs.map((j) => j.taskId).filter(Boolean),
    { ...deps, group },
    relayTimeoutMs(files),
    rids,
  );
  for (const j of jobs) {
    if (!j.taskId || done.get(j.taskId) !== 'done') {
      if (!failed.includes(j.name)) failed.push(`${j.name}（上传未完成）`);
    }
  }

  // 3) Locate each successful upload, then bridge it out.
  const okJobs = jobs.filter((j) => done.get(j.taskId) === 'done');
  const transferred = [];
  const claimed = new Set(preexisting || []);
  for (const j of okJobs) {
    const rid = rids.get(j.taskId) || await locateByName(deps, group, j.name, preexisting, claimed);
    if (!rid) { failed.push(`${j.name}（转存前定位失败）`); continue; }
    claimed.add(rid);
    try {
      await deps.apiPost(API.BRIDGE.TRANSFER, { resource_ids: [rid], group });
      transferred.push(j.name);
    } catch (e) {
      failed.push(`${j.name}（转存提交失败）`);
    }
  }

  return { total, uploaded: jobs.length, transferred: transferred.length, failed };
}

/** Ids present in the group before the relay. A full page means the listing
 * was truncated, so the snapshot comes back as null (= unknown) rather than
 * as an incomplete set that would let an older same-named file pass for the
 * fresh upload (same rule as utils/recover.js netdiskRows). A failed snapshot
 * keeps the legacy empty set: it only weakens the same-name disambiguation. */
async function snapshotIds(deps, group) {
  try {
    const r = await deps.apiGet(API.FILES.LIST, { group, page: 1, page_size: MAX_PAGE_SIZE });
    const items = (r.items || []).filter((it) => !it.is_dir);
    if (items.length >= MAX_PAGE_SIZE) return null; // truncated: unknown
    return new Set(items.map((it) => Number(it.id)));
  } catch (e) {
    return new Set();
  }
}

/**
 * Poll the task ledger until every task reaches a terminal state or the
 * deadline passes. Returns a map task_id -> terminal state.
 * @param {string[]} taskIds
 * @param {Object} deps - {apiPost, group}
 * @param {number} [timeoutMs]
 * @param {Map<string, number>} [ridSink] - filled with task_id -> resource_id
 *   when the ledger carries one (pins the exact resource, no name lookup)
 * @returns {Promise<Map<string, string>>}
 */
export async function waitTasksDone(taskIds, deps, timeoutMs, ridSink) {
  const result = new Map();
  const pending = new Set(taskIds);
  const deadline = Date.now() + (timeoutMs || BASE_TIMEOUT_MS);
  while (pending.size > 0 && Date.now() < deadline) {
    try {
      // task_ids 精确匹配：目标群台账超过一页（limit=100）时分页窗口可能
      // 不含自己的任务，按 id 过滤后不受截断影响（不再误判为超时失败）。
      const r = await deps.apiPost(API.TASKS, {
        target: deps.group, limit: 100, task_ids: [...pending],
      });
      for (const t of r?.tasks || []) {
        if (pending.has(t.task_id) && TERMINAL_STATES.has(t.state)) {
          result.set(t.task_id, t.state);
          const rid = ledgerResourceId(t);
          if (ridSink && rid) ridSink.set(t.task_id, rid);
          pending.delete(t.task_id);
        }
      }
    } catch (e) { /* transient poll failure: keep retrying */ }
    if (pending.size > 0) await sleep(POLL_MS);
  }
  return result;
}

/** Numeric resource id carried by a ledger row (absent for upload tasks,
 * which key by staged path); 0 when unusable. */
function ledgerResourceId(t) {
  const raw = t?.resource_id ?? t?.payload?.resource_id;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/**
 * Exact-name lookup among the group's files. Uploads of the same batch may
 * share a name (and the group may already hold one), so matches are ordered:
 * a new id that nothing has claimed yet wins; a listing that only offers an
 * already-known id still resolves (single-candidate fallback).
 * @param {Object} deps
 * @param {string} group
 * @param {string} name
 * @param {Set<number>|null} preexisting - null = unknown (truncated snapshot)
 * @param {Set<number>} claimed
 * @returns {Promise<number>} resource id, 0 when not found
 */
async function locateByName(deps, group, name, preexisting, claimed) {
  try {
    const r = await deps.apiGet(API.FILES.LIST, {
      group, q: name, page: 1, page_size: MAX_PAGE_SIZE,
    });
    const hits = (r.items || []).filter((it) => it.name === name && !it.is_dir);
    if (!hits.length) return 0;
    const free = hits.filter((it) => !claimed.has(Number(it.id)));
    if (preexisting) {
      // Ids known to predate the relay can never be the fresh upload: prefer
      // a candidate outside that set (the documented contract).
      const fresh = free.filter((it) => !preexisting.has(Number(it.id)));
      return Number((fresh[0] || free[0] || hits[0]).id);
    }
    // Truncated snapshot: which same-named resource is new is unknown, so
    // guessing among several candidates could transfer the old file.
    return free.length === 1 ? Number(free[0].id) : 0;
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
