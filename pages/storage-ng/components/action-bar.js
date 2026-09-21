/**
 * Action bar (D1-D15) - selection-driven command strip.
 *
 * Buttons come from the source's capability matrix mapped through the
 * command registry; each is disabled while its command is in flight
 * (mainstream busy state) or when preconditions fail. Execution goes
 * through the unified runCommand() lifecycle (commands.js).
 *
 * @module components/action-bar
 */

import { getState, subscribe, markBusy, unmarkBusy } from '../store.js';
import { getIcon } from '../icons.js';
import { formatSize } from '../utils/helpers.js';
import { resolveButtons, runCommand, canRunRowAware } from '../features/commands.js';
import { registerAllCommands } from '../features/command-registry.js';

// Command definitions register once at module load (idempotent).
registerAllCommands();

/**
 * Initialize an action bar bound to a data source.
 * @param {HTMLElement} container
 * @param {Object} source - DataSource adapter
 * @param {Object} [opts] - {onCountChange}
 * @returns {function} cleanup
 */
export function initActionBar(container, source, opts = {}) {
  container.className = 'file-action-bar';

  const render = () => renderBar(container, source);
  const subs = [
    subscribe(source.selectedKey, render),
    subscribe('busyKeys', render),
  ];

  container.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.disabled) return;
    // 契约: keys 必须由 rows 派生（同集同序），而不是选区全集——选区可能
    // 残留翻页/跨目录的 key 与目录行 key。command-defs*.js 用 ctx.keys[0]
    // 取单目标 id、用 ctx.rows[0] 取所属群，二者必须指向同一行；网盘的
    // 批量命令（netdiskRemovePaths / rename-batch / move-copy）直接消费
    // ctx.keys，因此 keys 里绝不能出现当前列表之外的路径。
    const selected = getState()[source.selectedKey] || new Set();
    const rows = (getState()[source.itemsKey] || [])
      .filter((f) => !f.is_dir && selected.has(source.rowKey(f)));
    const keys = rows.map((f) => source.rowKey(f));
    runCommand(btn.dataset.act, { source, keys, rows, rowAware: true }, {
      onBusy: (id) => markBusy(id),
      onDone: () => unmarkBusy(btn.dataset.act),
    });
    if (opts.onCountChange) opts.onCountChange();
  });

  render();
  return () => { subs.forEach((u) => u()); };
}

function renderBar(container, source) {
  const { busyKeys } = getState();
  const selected = getState()[source.selectedKey] || new Set();
  const count = selected.size;
  if (count === 0) {
    container.innerHTML = '<span class="bar-hint">选择文件后可执行操作</span>';
    return;
  }

  const items = getState()[source.itemsKey] || [];
  const totalSize = items
    .filter((f) => !f.is_dir && selected.has(source.rowKey(f)))
    .reduce((s, f) => s + (f.size || 0), 0);
  const rows = items.filter((f) => !f.is_dir && selected.has(source.rowKey(f)));
  const rowsHaveGroup = rows.some((f) => f.group_id);
  const hasGroup = Boolean(getState().currentGroup);

  const buttons = resolveButtons(source.capabilities, { count, hasGroup })
    .map(({ id, cmd, disabled, reason }) => {
      // Row-aware: group commands stay available when rows carry group_id.
      const eff = canRunRowAware(id, { count, hasGroup, rowsHaveGroup });
      const finalDisabled = disabled && !eff.ok;
      const busy = busyKeys.has(id);
      return `<button class="bar-btn ${cmd.danger ? 'danger' : ''}" data-act="${id}"` +
        `${(finalDisabled || busy) ? ' disabled' : ''} title="${finalDisabled ? reason : ''}">` +
        `${getIcon(cmd.icon || 'INFO', 13)} ${busy ? '处理中...' : (cmd.label || id)}</button>`;
    }).join('');

  container.innerHTML = `
    <span class="bar-info">已选 ${count} 项 (${formatSize(totalSize)})</span>
    <span class="bar-sep"></span>
    ${buttons}
  `;
}