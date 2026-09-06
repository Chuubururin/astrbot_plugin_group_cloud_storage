/**
 * Command layer - declarative action registry + unified execution.
 *
 * Every user-initiated operation is a Command object
 * {id, label, icon, danger, needsSingle, confirm?, run()} registered via
 * registerCommand(); runCommand() provides the standard lifecycle:
 * busy -> validate -> confirm -> execute -> toast -> refresh -> clear,
 * which eliminates hand-rolled try/catch + mutate + toast + refresh
 * scattered across views (single source of truth for capability mapping).
 *
 * @module features/commands
 */

import { getState, refresh } from '../store.js';
import { toast } from '../components/toast.js';
import { confirmEx } from '../components/modal.js';
import { recoverAfterFailure } from '../utils/recover.js';

const registry = new Map();

/**
 * Row-first group resolution now lives in utils/group.js (it is a data
 * concern, not a command concern); re-exported here so command
 * definitions and existing importers keep one stable import site.
 */
export { rowGroup, rowGroupFor, groupFor } from '../utils/group.js';

/**
 * Register a command definition (idempotent by id).
 * @param {Object} cmd - see module doc; run(ctx) must resolve on success
 */
export function registerCommand(cmd) {
  if (!cmd || !cmd.id) throw new Error(`command id required: ${cmd}`);
  registry.set(cmd.id, cmd);
}

/** Unregister a command (used by unit tests for ephemeral commands). */
export function unregisterCommand(id) {
  registry.delete(id);
}

/** All registered commands keyed by id. */
export function commands() {
  return Object.fromEntries(registry);
}

/**
 * Precondition check without executing.
 * @param {string} id
 * @param {{count: number, hasGroup: boolean}} env
 * @returns {{ok: boolean, reason?: string}}
 */
export function canRun(id, env) {
  const cmd = registry.get(id);
  if (!cmd) return { ok: false, reason: 'unknown command' };
  if (env.count === 0 && !cmd.allowNoSelection) return { ok: false, reason: 'no selection' };
  if (cmd.needsSingle && env.count !== 1) return { ok: false, reason: 'select exactly one item' };
  if (cmd.needsGroup && !env.hasGroup) return { ok: false, reason: 'select a group first' };
  return { ok: true };
}

/**
 * Row-aware precondition: in the aggregated  view rows carry their
 * own group_id, so group-dependent commands stay available without a
 * global currentGroup selection.
 */
export function canRunRowAware(id, env) {
  const base = canRun(id, env);
  if (base.ok) return base;
  const cmd = registry.get(id);
  if (cmd?.needsGroup && env.rowsHaveGroup) return { ok: true };
  return base;
}

/**
 * Resolve which buttons the action bar should render for a capability list.
 * Unknown capabilities are skipped - the capability list must only ever
 * reference registered commands (integration errors surface as missing
 * buttons, never as broken ones).
 * @param {string[]} capabilities - source capability ids
 * @param {{count: number, hasGroup: boolean}} env
 * @returns {Array<{id: string, cmd: Object, disabled: boolean, reason?: string}>}
 */
export function resolveButtons(capabilities, env) {
  return capabilities
    .map((id) => {
      const cmd = registry.get(id);
      if (!cmd) return null;
      const check = canRun(id, env);
      return { id, cmd, disabled: !check.ok, reason: check.reason };
    })
    .filter(Boolean);
}

/**
 * Execute a command through the standard lifecycle.
 *
 * @param {string} id - command id
 * @param {Object} ctx - runtime context given to run(): {source, keys, rows, state, rowAware}
 * @param {Object} hooks - {onBusy(id), onDone()}
 * @returns {Promise<void>}
 */
export async function runCommand(id, ctx, hooks = {}) {
  const cmd = registry.get(id);
  if (!cmd) { toast(`未知操作: ${id}`, 'error'); return; }

  const env = {
    count: (ctx.keys || []).length,
    hasGroup: Boolean(ctx.state?.currentGroup),
    rowsHaveGroup: (ctx.rows || []).some((r) => r.group_id),
  };
  const check = ctx.rowAware ? canRunRowAware(id, env) : canRun(id, env);
  if (!check.ok) { toast(check.reason || '条件不满足', 'warn'); return; }

  if (cmd.confirm) {
    const text = cmd.confirm(env.count);
    if (text) {
      const ok = await confirmEx(cmd.label || '操作', String(text),
        { okText: '确定', danger: Boolean(cmd.danger) });
      if (!ok) return;
    }
  }

  if (hooks.onBusy) hooks.onBusy(id);
  try {
    await cmd.run({ ...ctx, state: getState() });
    if (cmd.refresh) {
      const topics = Array.isArray(cmd.refresh) ? cmd.refresh : [cmd.refresh];
      for (const t of topics) refresh(t);
    }
    if (!cmd.keepSelection && ctx.source?.selection) ctx.source.selection.clear();
  } catch (e) {
    // Failure contract: besides the toast, re-pull the affected rows'
    // authoritative info from their cloud storage so the table never keeps
    // rendering a stale entry (opt out per command with recover: false).
    const sourceId = ctx.source?.id || 'group';
    const msg = `${cmd.label || id}失败: ${e.message || e}`;
    if (cmd.recover === false) {
      toast(msg, 'error');
    } else {
      await recoverAfterFailure(sourceId, ctx, msg);
    }
  } finally {
    if (hooks.onDone) hooks.onDone();
  }
}