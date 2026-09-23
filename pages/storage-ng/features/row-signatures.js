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
 * into markup, so "changed" means "the output would differ". Each module here
 * sits next to the builder it mirrors; keep them in sync:
 *   rowSignature   <- features/file-rows.js buildRow
 *   groupSignature <- features/group-data.js buildGroupRow
 *   (taskSignature lives in views/task-labels.js beside taskSummary)
 *
 * @module features/row-signatures
 */

/** U+0000 separator: never present in the rendered strings below. */
const SEP = '\u0000';

/**
 * Resource table row (group files / albums / essence / netdisk).
 * @param {Object} item
 * @returns {string}
 */
export function rowSignature(item) {
  return [
    item.is_up ? 1 : 0, item.is_dir ? 1 : 0, item.is_folder ? 1 : 0,
    item.name, item.type, item.size, item.uploader, item.modified, item.created,
    item.is_volume ? 1 : 0, item.volume_total, item.volume_complete ? 1 : 0,
    item.volume_done, item.is_long ? 1 : 0, item.indexed_at, item.tags,
  ].join(SEP);
}

/**
 * Group table row. Selection state is deliberately absent: it lives in
 * `selectedGroups` and is applied by updateCheckboxes without a row rebuild.
 * @param {Object} g
 * @returns {string}
 */
export function groupSignature(g) {
  return [
    g.shown_name, g.group_name, g.label, g.role, g.total_space, g.limit_count,
    g.used_space, g.album_count, g.essence_count, g.last_scan_at, g.last_scan,
  ].join(SEP);
}
