/**
 * Whole-DTO row equality - the fallback comparator of utils/dom-diff.
 *
 * Only used when a caller does not declare `signatureFn`: this compares every
 * field of both DTOs, so a server-side field the row never renders still
 * forces a replacement. The signature path (a string projection of the fields
 * actually rendered) is the accurate one, and dom-diff warns once per
 * container when it is missing.
 *
 * @module utils/dto-equal
 */

/**
 * Value equality for one field: scalars by identity, arrays/objects by JSON
 * projection (JSON.parse yields a fresh reference per poll, so identity alone
 * would mark every nested field changed).
 */
function sameValue(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return false;
  if (typeof a !== 'object' || typeof b !== 'object') return false;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
}

/** Field-level equality across the union of both objects' keys. */
export function hasChanged(oldItem, newItem) {
  const keys = new Set([...Object.keys(oldItem), ...Object.keys(newItem)]);
  for (const key of keys) {
    if (!sameValue(oldItem[key], newItem[key])) return true;
  }
  return false;
}
