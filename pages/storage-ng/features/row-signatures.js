/**
 * Row render signatures - the "what the row actually displays" contract that
 * feeds applyKeyedDiff's signatureFn slot.
 *
 * Keyed diffing has to decide whether a row changed. Comparing the whole DTO
 * answers the wrong question: the resource/group DTOs carry server-side state
 * the table never renders (meta, payload, local paths, scan bookkeeping), so a
 * poll that only touched those fields still rewrote every row - a fresh
 * element, a lost tooltip target, and a full re-bind of the row listeners.
 *
 * A signature projects the row down to exactly the values its builder turns
 * into markup, so "changed" means "the output would differ". Every projection
 * goes through joinSignature, which is what keeps that claim true for container
 * values too. Each module here sits next to the builder it mirrors; keep them
 * in sync:
 *   rowSignature   <- features/file-rows.js buildRow
 *   groupSignature <- features/group-data.js buildGroupRow
 *   (taskSignature lives in views/task-labels.js beside taskSummary)
 *
 * @module features/row-signatures
 */

/** U+0000 separator: never present in the values projected below. */
const SEP = '\u0000';

/**
 * Join projected values, serializing containers.
 *
 * `Array.prototype.join` renders every plain object as "[object Object]", so a
 * projected field that ever holds one is invisible to the diff forever: the row
 * keeps stale DOM and no test can see it. JSON.stringify makes nested content
 * observable; null/undefined still join to the empty string, and only the rare
 * container pays (the resource payload sends `tags` as a list).
 * @param {Array} parts
 * @returns {string}
 */
export function joinSignature(parts) {
  const atoms = new Array(parts.length);
  for (let i = 0; i < parts.length; i++) {
    const v = parts[i];
    atoms[i] = v !== null && typeof v === 'object' ? JSON.stringify(v) : v;
  }
  return atoms.join(SEP);
}

/**
 * Resource table row (group files / albums / essence / netdisk).
 * @param {Object} item
 * @returns {string}
 */
export function rowSignature(item) {
  return joinSignature([
    item.is_up ? 1 : 0, item.is_dir ? 1 : 0, item.is_folder ? 1 : 0,
    item.name, item.type, item.size, item.uploader, item.modified, item.created,
    item.is_volume ? 1 : 0, item.volume_total, item.volume_complete ? 1 : 0,
    item.volume_done, item.is_long ? 1 : 0, item.indexed_at, item.tags,
  ]);
}

/**
 * Group table row. Selection state is deliberately absent: it lives in
 * `selectedGroups` and is applied by updateCheckboxes without a row rebuild.
 * @param {Object} g
 * @returns {string}
 */
export function groupSignature(g) {
  return joinSignature([
    g.shown_name, g.group_name, g.label, g.role, g.total_space, g.limit_count,
    g.used_space, g.album_count, g.essence_count, g.last_scan_at, g.last_scan,
  ]);
}
