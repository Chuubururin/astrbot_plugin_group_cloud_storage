"""Resources domain — CRUD, query, stats, URI, tags.

``resource_id`` DERIVES from the session-scoped NapCat file_id, so it is not a
stable identity. ``logical_key`` (``group:type:name``) is, behind a PARTIAL
unique index (``WHERE status != 'deleted'``): one live row per logical file,
tombstones retained. See ``upsert_resources``.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .state import StorePart
from .like import like_contains, like_escape
from .row_identity import _logical_key, _logical_path, _path_for
from .status_policy import resource_status_sql
from typing import TYPE_CHECKING
from core.domain.enums import ResourceStatus, ResourceType
from core.domain.resource import Resource
from core.domain.sync import Page, PageItem, ResourceQuery, ResourceStats

if TYPE_CHECKING:
    from .connection import ConnectionManager

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


class ResourcesMixin(StorePart):
    """Resource CRUD and query operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"
        _tag_cloud_cache: dict

    def _inherit_composition_identity(
        self, conn: sqlite3.Connection, items: list[Resource]
    ) -> None:
        """Safety net for composition parents that predate ``logical_key``.

        Normally the ON CONFLICT(logical_key) clause collapses a restarted
        session's file_id onto the live row, so nothing stale forms here. It
        stays for rows already duplicated (v29 keeps their losers as
        tombstones) and as a guard if one ever escapes the index.

        A stale same-(group_id, name) parent hands its ``meta`` to the
        successor and is TOMBSTONED, never hard-removed -- the partial index
        tolerates history. Rows whose ``source_ref`` is still listed are left
        alone: two distinct cloud files may share a name in one folder, and
        only a vanished session handle proves identity.
        """
        if not any(
            r.type == ResourceType.FILE and r.source_ref for r in items
        ):
            return
        # Indexed by idx_res_name; composition parents are rare per group.
        comp_rows = conn.execute(
            """
            SELECT resource_id, group_id, name, source_ref, meta FROM resources
            WHERE type='file'
              -- Both parent shapes: canonical composition.kind and the
              -- legacy {'volumes': true} from ingest/video.py. json_valid
              -- first -- json_extract raises on a non-NULL invalid meta and
              -- would abort the whole batch.
              AND json_valid(meta)
              AND (json_extract(meta, '$.composition.kind') = 'volumes'
                   OR json_extract(meta, '$.volumes') = 1)
            """
        ).fetchall()
        if not comp_rows:
            return
        incoming_refs = {r.source_ref for r in items if r.source_ref}
        # Successor lookup: newest incoming row wins when the same name is
        # listed several times in one batch.
        successors: dict[tuple[str, str], Resource] = {}
        for r in items:
            if r.type == ResourceType.FILE and r.source_ref:
                successors[(r.group_id, r.name)] = r
        for pred in comp_rows:
            key = (pred["group_id"], pred["name"])
            succ = successors.get(key)
            if (
                succ is None
                or succ.resource_id == pred["resource_id"]
                or pred["source_ref"] in incoming_refs
            ):
                continue
            pred_id, pred_meta = pred["resource_id"], pred["meta"]
            try:
                merged = json.loads(pred_meta) if pred_meta else {}
            except (TypeError, ValueError):
                merged = {}
            conn.execute(
                "UPDATE resources SET meta=? WHERE resource_id=?",
                (json.dumps(merged, ensure_ascii=False), succ.resource_id),
            )
            # BEFORE tombstoning -- parent_resource_id has no FK backstop.
            conn.execute(
                "UPDATE volumes SET parent_resource_id=? WHERE parent_resource_id=?",
                (succ.resource_id, pred_id),
            )
            # Tombstone, not DELETE: the partial index tolerates history.
            conn.execute(
                "UPDATE resources SET status=?, size=0, updated_at=? "
                "WHERE resource_id=?",
                (ResourceStatus.DELETED.value, int(time.time()), pred_id),
            )

    async def upsert_resources(self, items: list[Resource]) -> int:
        if not items:
            return 0
        now = int(time.time())

        def _do(conn: sqlite3.Connection):
            moves = _plan_reparents(conn, items)
            rows = []
            for r in items:
                path, ext = _logical_path(r)
                rows.append(
                    (
                        r.resource_id,
                        r.group_id,
                        r.type.value,
                        r.name,
                        r.size,
                        r.uploader_id,
                        r.uploader_name,
                        r.source_ref,
                        r.busid,
                        r.folder_id,
                        r.folder_name,
                        r.status.value,
                        json.dumps(r.tags, ensure_ascii=False),
                        json.dumps(r.meta, ensure_ascii=False),
                        r.created_at,
                        now,
                        now,
                        path,
                        ext,
                        _logical_key(r),
                    )
                )
            n = 0
            for i in range(0, len(rows), 500):
                chunk = rows[i : i + 500]
                # No explicit BEGIN: sqlite3 auto-begins before DML and a
                # second BEGIN conflicts with the one already in progress.
                try:
                    cur = conn.executemany(
                        f"""
                        INSERT INTO resources
                          (resource_id, group_id, type, name, size, uploader_id,
                           uploader_name, source_ref, busid, folder_id, folder_name,
                           status, tags, meta, created_at, indexed_at, updated_at,
                           path, ext, logical_key)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        -- Two targets, both required: a violation reaches
                        -- only a NAMED handler, and the partial predicate
                        -- spares tombstones so a file can return. Both bodies
                        -- are identical because SQLite applies the FIRST
                        -- matching clause and skips the rest -- a thin
                        -- resource_id clause would shadow the full one on a
                        -- same-session re-index. See _RESOURCE_CONFLICT_BODY.
                        ON CONFLICT(resource_id) DO UPDATE SET
{_RESOURCE_CONFLICT_BODY}

                        ON CONFLICT(group_id, type, logical_key)
                          WHERE status != 'deleted'
                        DO UPDATE SET
{_RESOURCE_CONFLICT_BODY}
                        """,
                        chunk,
                    )
                    n += cur.rowcount
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            # AFTER every chunk on purpose: mid-loop it would touch successor
            # rows a later chunk has not inserted yet, and drop their meta.
            try:
                self._inherit_composition_identity(conn, items)
                # Same transaction: a half-moved set orphans parts for good.
                if moves:
                    _reparent_volumes(conn, moves)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return n

        result = await self._conn.exec(_do)
        self._tag_cloud_cache = {}
        return result

    async def get_by_uri(self, uri: str) -> dict | None:
        """Resolve ``cloud://<group_id>/<type>/<id>`` to an active row.

        group_id and type are part of the URI identity, not decoration: the
        URI is a public, encodable handle (webapi/resources_mutation.py
        exposes it), so matching on the trailing id alone let
        ``cloud://<any group>/<any type>/<id>`` reach a row belonging to
        another group. A mismatch reads as "not found" (None), the same
        convention as an unknown id; malformed URIs still raise ValueError.
        """
        prefix = "cloud://"
        if not uri.startswith(prefix):
            raise ValueError("invalid resource uri")
        parts = uri[len(prefix) :].split("/")
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("invalid resource uri")
        group_id, rtype, sid = parts[0], parts[1], int(parts[2])

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id=? AND status='active' "
                "AND group_id=? AND type=?",
                (sid, group_id, rtype),
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def query_resources(self, q: ResourceQuery, fold_parts: bool = True) -> Page:
        """fold_parts=False lets name-based backfill (e.g. writing volume
        source_refs back) bypass part folding -- list semantics fold parts,
        but storage-layer name lookups must still see the part rows."""

        def _do(conn: sqlite3.Connection):
            where = ["status = ?"] + ([_PART_FOLD_COND] if fold_parts else [])
            params: list = [q.status]
            if q.type:
                where.append("type = ?")
                params.append(q.type)
            # None = no filter; [] = match nothing. `if q.groups:` treated []
            # as falsy and dropped the filter, so the wither scope (all
            # accounts offline) leaked every group instead of returning none.
            if q.groups is not None:
                if q.groups:
                    marks = ",".join("?" for _ in q.groups)
                    where.append(f"group_id IN ({marks})")
                    params.extend(q.groups)
                else:
                    where.append("1=0")
            elif q.group_id:
                where.append("group_id = ?")
                params.append(q.group_id)
            if q.keyword:
                where.append(
                    "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
                    "OR folder_name LIKE ? ESCAPE '\\' "
                    "OR ext LIKE ? ESCAPE '\\' OR mime LIKE ? ESCAPE '\\' "
                    "OR uploader_name LIKE ? ESCAPE '\\' "
                    "OR uploader_id LIKE ? ESCAPE '\\' OR sha256 LIKE ? ESCAPE '\\' "
                    "OR source_ref LIKE ? ESCAPE '\\' "
                    "OR lower(COALESCE("
                    "CASE WHEN json_valid(meta) "
                    "THEN json_extract(meta, '$.summary') END, '')) "
                    "LIKE ? ESCAPE '\\' "
                    "OR group_id LIKE ? ESCAPE '\\' "
                    "OR group_id IN ("
                    "  SELECT g.group_id FROM groups g "
                    "  WHERE g.group_name LIKE ? ESCAPE '\\'"
                    "))"
                )
                # Escape wildcards: an unescaped `_`/`%` in the keyword matched
                # far more rows than the user typed.
                keyword_like = like_contains(q.keyword)
                params.extend([keyword_like] * 12)
            if q.uploader_id:
                where.append("uploader_id = ?")
                params.append(q.uploader_id)
            if q.folder_id is not None:
                where.append("folder_id = ?")
                params.append(q.folder_id)
            if q.ids is not None:
                if q.ids:
                    marks = ",".join("?" for _ in q.ids)
                    where.append(f"id IN ({marks})")
                    params.extend(q.ids)
                else:
                    where.append("1=0")
            if q.folder == "__root__":
                where.append("(folder_name IS NULL OR folder_name = '')")
            elif q.folder:
                where.append("folder_name = ?")
                params.append(q.folder)
            if q.store_status:
                # Derived cross-reference filters. The Files tab keeps
                # type='file' rows and asks whether the file also lives in
                # another store (never returns album/essence rows themselves —
                # those are the other tabs' content). For album/essence the
                # id-linkage is absent (distribute pipelines leave no FK), so
                # the fall-back from the requirement doc applies: a same-name
                # resource of the target type in the same group.
                if q.store_status == "netdisk":
                    where.append(
                        "EXISTS (SELECT 1 FROM archive_map am "
                        "WHERE am.resource_id = resources.id "
                        "AND am.direction = 'out' AND am.state = 'done')"
                    )
                elif q.store_status == "album":
                    where.append(
                        "EXISTS (SELECT 1 FROM resources o WHERE o.type = 'album' "
                        "AND o.group_id = resources.group_id "
                        "AND o.name = resources.name AND o.status = 'active' "
                        "AND o.id != resources.id)"
                    )
                elif q.store_status == "essence":
                    where.append(
                        "EXISTS (SELECT 1 FROM resources o WHERE o.type = 'essence' "
                        "AND o.group_id = resources.group_id "
                        "AND o.name = resources.name AND o.status = 'active' "
                        "AND o.id != resources.id)"
                    )
                elif q.store_status == "none":
                    where.append(
                        "NOT EXISTS ("
                        "SELECT 1 FROM archive_map am WHERE am.resource_id = resources.id "
                        "AND am.direction = 'out' AND am.state = 'done')"
                        " AND NOT EXISTS ("
                        "SELECT 1 FROM resources o WHERE o.type IN ('album', 'essence') "
                        "AND o.group_id = resources.group_id "
                        "AND o.name = resources.name AND o.status = 'active' "
                        "AND o.id != resources.id)"
                    )
            if q.exts:
                # "%{ext}" matches the plain suffix; "%{ext}.%" also matches
                # transient intermediates ("x.rar.netdisk.p.downloading") that
                # classify() strips down to the real extension, so type
                # filtering stays a superset of the displayed classification.
                clauses: list[str] = []
                for e in q.exts:
                    # Escaped like `keyword`: extensions come from the type
                    # table + config overrides, but a `_`/`%` inside one
                    # turned into a wildcard and matched unrelated names.
                    el = like_escape(e.lower())
                    clauses.append("LOWER(name) LIKE ? ESCAPE '\\'")
                    params.append(f"%{el}")
                    clauses.append("LOWER(name) LIKE ? ESCAPE '\\'")
                    params.append(f"%{el}.%")
                where.append(f"({' OR '.join(clauses)})")
            if q.tags:
                for tag in q.tags:
                    # Tag text is user-typed (#tag tokens in the search
                    # box), so wildcards must be escaped.
                    where.append("tags LIKE ? ESCAPE '\\'")
                    params.append(f'%"{like_escape(tag)}"%')
            cond = " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) FROM resources WHERE {cond}", params
            ).fetchone()[0]
            offset = (q.page - 1) * q.page_size
            sort_map = {
                "id": "id",
                "name": "name",
                "size": "size",
                "created_at": "created_at",
                "uploader_name": "uploader_name",
            }
            order_by = sort_map.get(q.sort_by, "id")
            order_dir = "DESC" if q.sort_dir == "desc" else "ASC"
            rows = conn.execute(
                f"""
                SELECT id, resource_id, group_id, name, size, uploader_id,
                       uploader_name, folder_name, created_at, indexed_at,
                       busid, source_ref, meta, type, tags, path, ext
                FROM resources WHERE {cond}
                ORDER BY {order_by} {order_dir}, id LIMIT ? OFFSET ?
                """,
                [*params, q.page_size, offset],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                meta = d.get("meta")
                d["meta"] = json.loads(meta) if meta else None
                tags = d.get("tags")
                d["tags"] = json.loads(tags) if tags else []
                items.append(PageItem(**d))
            return Page(items=items, total=total, page=q.page, page_size=q.page_size)

        return await self._conn.exec(_do)

    async def get_resource_detail(self, group_id: str, id: int) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND group_id = ? AND type='file'",
                (id, group_id),
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def stats(self, group_id: str) -> ResourceStats:
        def _do(conn: sqlite3.Connection):
            # Stats use the same filtering as the list: volume part folding
            # (see _PART_FOLD_COND)
            cond = f"group_id=? AND type='file' AND status='active' AND {_PART_FOLD_COND}"
            row = conn.execute(
                f"SELECT COUNT(*) c, COALESCE(SUM(size),0) s, COUNT(DISTINCT uploader_id) u "
                f"FROM resources WHERE {cond}",
                (group_id,),
            ).fetchone()
            return ResourceStats(
                group_id=group_id,
                file_count=row["c"],
                total_size=row["s"],
                uploaders=row["u"],
                by_folder=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT folder_name, COUNT(*) cnt, SUM(size) bytes FROM resources "
                        f"WHERE {cond} GROUP BY folder_id ORDER BY bytes DESC LIMIT 10",
                        (group_id,),
                    )
                ],
                by_uploader=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT uploader_id, uploader_name, COUNT(*) cnt, SUM(size) bytes "
                        f"FROM resources WHERE {cond} GROUP BY uploader_id "
                        f"ORDER BY bytes DESC LIMIT 10",
                        (group_id,),
                    )
                ],
                recent_7d=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT date(created_at,'unixepoch') d, COUNT(*) cnt FROM resources "
                        f"WHERE {cond} AND created_at >= ? GROUP BY d ORDER BY d",
                        (group_id, int(time.time()) - 7 * 86400),
                    )
                ],
            )

        return await self._conn.exec(_do)

    async def sum_resource_sizes(self, group_id: str) -> int:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM resources "
                "WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchone()
            return int(row[0] or 0)

        return await self._conn.exec(_do)

    async def update_resource_fields(self, id: int, **fields) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - _RESOURCE_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid resource fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            cur = conn.execute(
                f"UPDATE resources SET {sets}, updated_at=? WHERE id=?",
                [*fields.values(), int(time.time()), id],
            )
            if "name" in fields:
                row = conn.execute(
                    "SELECT group_id, type, name, folder_name FROM resources WHERE id=?",
                    (id,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE resources SET path=? WHERE id=?",
                        (_path_for(row["group_id"], row["type"], row["name"],
                                   row["folder_name"]), id),
                    )
                    conn.execute(_RENAME_LOGICAL_KEY, (id,))
            conn.commit()
            return cur.rowcount

        await self._conn.exec(_do)
        self._tag_cloud_cache = {}

    async def update_resource_tags(self, id: int, tags: list[str]) -> None:
        cleaned = sorted({str(t).strip() for t in tags if str(t).strip()})
        await self.update_resource_fields(
            id, tags=json.dumps(cleaned, ensure_ascii=False)
        )

    async def get_resource_by_resource_id(self, resource_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def get_resource_any(self, id: int) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id=? AND status='active'", (id,)
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def mark_missing_as_deleted(
        self, group_id: str, complete: bool, source_file_ids: set[str]
    ) -> int:
        """Mark active files absent from a complete source listing as deleted.

        The source IDs are staged in a temporary table instead of being split into
        ``NOT IN`` batches.  Batching changes the meaning of the predicate (a row
        present in a later batch can be deleted by an earlier batch), and also
        made an empty complete listing impossible to represent.
        """
        if not complete:
            return 0

        def _do(conn: sqlite3.Connection):
            # BUG-2 fix: removed explicit BEGIN — Python sqlite3 auto-begins
            # transactions before DML; an explicit BEGIN would conflict with
            # any implicit transaction already in progress (OperationalError).
            # The ConnectionManager._run() rolls back uncommitted transactions
            # in its finally block, providing the safety net.
            try:
                conn.execute("CREATE TEMP TABLE sync_source_ids (source_ref TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO sync_source_ids(source_ref) VALUES (?)",
                    ((str(source_ref),) for source_ref in source_file_ids),
                )
                cur = conn.execute(
                    """
                    UPDATE resources SET status=?, updated_at=?
                    WHERE group_id=? AND type='file' AND status='active'
                      AND NOT EXISTS (
                          SELECT 1 FROM sync_source_ids s
                          WHERE s.source_ref = resources.source_ref
                      )
                      -- NOT json_valid(meta): a corrupt row cannot prove it
                      -- is a volume parent, and json_extract would raise
                      -- "malformed JSON" and fail the whole sweep.
                      AND (meta IS NULL OR NOT json_valid(meta)
                           OR COALESCE(json_extract(meta, '$.volumes'), 0) != 1)
                    """,
                    [ResourceStatus.DELETED.value, int(time.time()), group_id],
                )
                n = cur.rowcount
                conn.execute("DROP TABLE sync_source_ids")
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                # Rollback removes the temporary table creation as well, but drop
                # defensively for connection implementations that retain temp DDL.
                conn.execute("DROP TABLE IF EXISTS sync_source_ids")
                raise

        result = await self._conn.exec(_do)
        if result:
            self._tag_cloud_cache = {}
        return result

    async def count_active(self, group_id: str) -> int:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchone()
            return int(row[0] or 0)

        return await self._conn.exec(_do)


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
