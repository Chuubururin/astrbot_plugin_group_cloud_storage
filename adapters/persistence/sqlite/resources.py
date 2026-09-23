"""Resources domain shared constants and row helpers (parts: write/query/get).

``resource_id`` DERIVES from the session-scoped NapCat file_id, so it is not a
stable identity. ``logical_key`` (``group:type:name``) is, behind a PARTIAL
unique index (``WHERE status != 'deleted'``): one live row per logical file,
tombstones retained. See ``resources_write._apply_upsert``.

The mixin bodies live in:
- ``resources_write.py``  ResourceWriteMixin (upsert / album-essence / update)
- ``resources_query.py``  ResourceQueryMixin (paged query / stats)
- ``resources_get.py``    ResourceGetMixin (point reads)
"""
from __future__ import annotations

import json
import sqlite3

from .status_policy import resource_status_sql

from core.domain.enums import ResourceType
from core.domain.resource import Resource

_RESOURCE_FIELD_WHITELIST = frozenset(
    {"name", "folder_id", "folder_name", "status", "size", "mime", "meta", "tags"}
)

# Shared ON CONFLICT body. Both targets need it: SQLite applies the FIRST
# matching clause and skips the rest, so the two must not drift.
_RESOURCE_CONFLICT_BODY = f"""\
  resource_id=excluded.resource_id,
  source_ref=excluded.source_ref,
  name=excluded.name, size=excluded.size,
  uploader_id=COALESCE(excluded.uploader_id, uploader_id),
  uploader_name=COALESCE(excluded.uploader_name, uploader_name),
  busid=COALESCE(excluded.busid, busid),
  folder_id=excluded.folder_id, folder_name=excluded.folder_name,
  created_at=CASE WHEN excluded.created_at > 0
      THEN excluded.created_at ELSE created_at END,
  status={resource_status_sql()},
  -- json_valid first: json_extract raises on a non-NULL invalid meta.
  -- '$.volumes'=1 covers the legacy volts-only shape (no composition key)
  -- written by ingest/video.py; without it that meta was overwritten.
  meta=CASE WHEN resources.meta IS NOT NULL AND json_valid(resources.meta)
      AND (json_extract(resources.meta, '$.composition') IS NOT NULL
           OR json_extract(resources.meta, '$.volumes') = 1)
    THEN json_patch(resources.meta, excluded.meta) ELSE excluded.meta END,
  updated_at=excluded.updated_at,
  path=excluded.path, ext=excluded.ext"""

# ``logical_key`` is what the partial unique index enforces, so a rename must
# refresh it: left stale, the OLD name's return matched this row and overwrote
# it (a.mp4 -> b.mp4, then a.mp4 returned and b.mp4 was lost). Guarded --
# renaming onto a name a live row holds would violate the index, so the key is
# then left alone and the next upsert reconciles through the row key.
_RENAME_LOGICAL_KEY = (
    "UPDATE resources SET logical_key = CASE WHEN NOT EXISTS ("
    "  SELECT 1 FROM resources o WHERE o.group_id=resources.group_id "
    "    AND o.type=resources.type AND o.name=resources.name "
    "    AND o.id != resources.id AND o.status != 'deleted') "
    "THEN group_id||':'||type||':'||name ELSE logical_key END "
    "WHERE id=?"
)

# A row whose name is a registered part_name with a live parent is hidden from
# list/search/stats, so parts never appear twice beside their parent. They
# resurface once the parent is deleted, for manual cleanup.
_PART_FOLD_COND = (
    "NOT EXISTS ("
    "SELECT 1 FROM volumes v JOIN resources p ON p.resource_id = v.parent_resource_id "
    "WHERE v.part_name = resources.name "
    "AND substr(v.parent_resource_id, 1, instr(v.parent_resource_id, ':') - 1)"
    "     = resources.group_id)"
)


def _plan_reparents(
    conn: sqlite3.Connection, items: list[Resource]
) -> list[tuple[str, str]]:
    """Pairs of (old, new) resource_id for every logical key being collapsed.

    An ON CONFLICT(logical_key) hit updates the live row in place, rewriting
    resource_id (which derives from source_ref). Children keyed by the old id
    must be chased. Snapshot BEFORE the upsert, since the old id is gone after.
    """
    moves: list[tuple[str, str]] = []
    for r in items:
        if r.type != ResourceType.FILE or not r.source_ref:
            continue
        row = conn.execute(
            "SELECT resource_id FROM resources "
            "WHERE group_id=? AND type=? AND name=? AND status != 'deleted'",
            (r.group_id, r.type.value, r.name),
        ).fetchone()
        if row and row[0] != r.resource_id:
            moves.append((row[0], r.resource_id))
    return moves


def _reparent_volumes(
    conn: sqlite3.Connection, moves: list[tuple[str, str]]
) -> int:
    """Move ``volumes.parent_resource_id`` from an old id to a new one.

    ``parent_resource_id`` is a plain TEXT column with no FK backstop, so a
    rewritten parent id orphans its children *silently* -- the part-fold JOIN
    simply stops matching. Returns the number of rows moved, which is also the
    only cheap signal that the chase was needed at all.
    """
    moved = 0
    for old_id, new_id in moves:
        if old_id == new_id:
            continue
        cur = conn.execute(
            "UPDATE volumes SET parent_resource_id=? WHERE parent_resource_id=?",
            (new_id, old_id),
        )
        moved += cur.rowcount
    return moved


def _row_with_meta(row) -> dict:
    d = dict(row)
    if d.get("meta"):
        try:
            d["meta"] = json.loads(d["meta"])
        except ValueError:
            pass
    return d
