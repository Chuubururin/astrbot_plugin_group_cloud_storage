"""Resources domain — CRUD, query, stats, URI, tags."""
from __future__ import annotations

import json
import sqlite3
import time

from .state import StorePart
from typing import TYPE_CHECKING

from core.domain.enums import ResourceStatus, ResourceType
from core.domain.resource import Resource
from core.domain.sync import Page, PageItem, ResourceQuery, ResourceStats

if TYPE_CHECKING:
    from .connection import ConnectionManager

_RESOURCE_FIELD_WHITELIST = frozenset(
    {"name", "folder_id", "folder_name", "status", "size", "mime", "meta", "tags"}
)

# Volume part folding (list semantics): rows whose name matches a part_name
# registered in volumes and whose logical parent entry still exists are hidden
# from list/search/stats -- listings show only the logical large-file entry,
# so parts do not appear twice alongside their parent. Parts become visible
# again once the parent is deleted, for manual cleanup.
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
        """Successor-row identity carry-over (same logical file, new file_id).

        NapCat file_ids are session-scoped handles: after a restart the same
        cloud file lists under a fresh file_id, so the sync inserts a new
        resource_id row (meta empty) while the composition-carrying row keeps
        the same (group, name) identity. The sweep cannot retire the old row
        (its volumes-guard keeps composition parents), leaving two rows for
        one logical file and a 0/n volume view on the active one.

        One indexed pass loads every volume-composition row; each incoming
        file row then adopts a stale same-(group_id, name) row's composition
        meta — stale means its source_ref is no longer among the incoming
        listing (the cloud no longer exposes that session's file_id).
        Volumes are reattached to the successor and the stale row is
        hard-removed so (group, name) identity stays unique. Rows whose
        source_ref is still listed keep their data: genuinely distinct cloud
        files with identical names in one folder are merged only when the old
        session handle has vanished.
        """
        if not any(
            r.type == ResourceType.FILE and r.source_ref for r in items
        ):
            return
        # O(composition rows) via idx_res_name — composition parents are rare
        # (a handful per group), so this stays small even on 20k upserts.
        comp_rows = conn.execute(
            """
            SELECT resource_id, group_id, name, source_ref, meta FROM resources
            WHERE type='file'
              AND json_extract(meta, '$.composition.kind') = 'volumes'
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
            # Reattach volume records to the successor BEFORE the predecessor
            # row is removed (volumes rows are keyed by parent resource_id).
            conn.execute(
                "UPDATE volumes SET parent_resource_id=? WHERE parent_resource_id=?",
                (succ.resource_id, pred_id),
            )
            # The predecessor is superseded: hard-remove so (group, name)
            # identity stays unique and it cannot re-shade listings.
            conn.execute("DELETE FROM resources WHERE resource_id=?", (pred_id,))

    async def upsert_resources(self, items: list[Resource]) -> int:
        if not items:
            return 0
        now = int(time.time())

        def _do(conn: sqlite3.Connection):
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
                    )
                )
            n = 0
            for i in range(0, len(rows), 500):
                chunk = rows[i : i + 500]
                conn.execute("BEGIN")
                try:
                    cur = conn.executemany(
                        """
                        INSERT INTO resources
                          (resource_id, group_id, type, name, size, uploader_id,
                           uploader_name, source_ref, busid, folder_id, folder_name,
                           status, tags, meta, created_at, indexed_at, updated_at,
                           path, ext)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(resource_id) DO UPDATE SET
                          name=excluded.name, size=excluded.size,
                          uploader_id=COALESCE(excluded.uploader_id, uploader_id),
                          uploader_name=COALESCE(excluded.uploader_name, uploader_name),
                          busid=COALESCE(excluded.busid, busid),
                          folder_id=excluded.folder_id, folder_name=excluded.folder_name,
                          created_at=CASE WHEN excluded.created_at > 0
                              THEN excluded.created_at ELSE created_at END,
                          status=CASE WHEN excluded.status != 'active'
                              THEN excluded.status ELSE status END,
                          meta=CASE WHEN resources.meta IS NOT NULL
                              AND json_extract(resources.meta, '$.composition') IS NOT NULL
                            THEN json_patch(resources.meta, excluded.meta)
                            ELSE excluded.meta END,
                          updated_at=excluded.updated_at,
                          path=excluded.path, ext=excluded.ext
                        """,
                        chunk,
                    )
                    n += cur.rowcount
                    # Successor identity carry-over: a newly listed file_id for
                    # a known (group, name) adopts the deleted composition
                    # row's meta (volume/composition identity survives the
                    # session-scoped file_id churn).
                    self._inherit_composition_identity(conn, items)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return n

        result = await self._conn.exec(_do)
        self._tag_cloud_cache = {}
        return result

    async def get_by_uri(self, uri: str) -> dict | None:
        prefix = "cloud://"
        if not uri.startswith(prefix):
            raise ValueError("invalid resource uri")
        parts = uri[len(prefix) :].split("/")
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("invalid resource uri")
        return await self.get_resource_any(int(parts[2]))

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
            if q.groups:
                marks = ",".join("?" for _ in q.groups)
                where.append(f"group_id IN ({marks})")
                params.extend(q.groups)
            elif q.group_id:
                where.append("group_id = ?")
                params.append(q.group_id)
            if q.keyword:
                where.append(
                    "(name LIKE ? OR path LIKE ? OR folder_name LIKE ? "
                    "OR ext LIKE ? OR mime LIKE ? OR uploader_name LIKE ? "
                    "OR uploader_id LIKE ? OR sha256 LIKE ? OR source_ref LIKE ? "
                    "OR lower(COALESCE(json_extract(meta, '$.summary'), '')) LIKE ? "
                    "OR group_id LIKE ? "
                    "OR group_id IN ("
                    "  SELECT g.group_id FROM groups g "
                    "  WHERE g.group_name LIKE ?"
                    "))"
                )
                keyword_like = f"%{q.keyword}%"
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
                ext_where = " OR ".join(["LOWER(name) LIKE ?" for _ in q.exts])
                where.append(f"({ext_where})")
                params.extend([f"%{e.lower()}" for e in q.exts])
            if q.tags:
                for tag in q.tags:
                    where.append("tags LIKE ?")
                    params.append(f'%"{tag}"%')
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
                conn.execute(
                    "UPDATE resources SET path = CASE "
                    "WHEN type = 'file' AND folder_name IS NOT NULL AND folder_name != '' "
                    "THEN '/' || group_id || '/' || folder_name || '/' || name "
                    "WHEN type = 'file' THEN '/' || group_id || '/' || name "
                    "WHEN type = 'album' THEN '/' || group_id || '/__album__/' || name "
                    "WHEN type = 'essence' THEN '/' || group_id || '/__essence__/' || name "
                    "ELSE path END WHERE id=?",
                    (id,),
                )
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
                      AND (meta IS NULL OR meta NOT LIKE '%"volumes": true%')
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


def _logical_path(r) -> tuple[str, str]:
    t = r.type.value if hasattr(r.type, "value") else str(r.type)
    if t == "file":
        if r.folder_name:
            path = f"/{r.group_id}/{r.folder_name}/{r.name}"
        else:
            path = f"/{r.group_id}/{r.name}"
        dot = r.name.rfind(".")
        ext = r.name[dot + 1 :].lower() if dot > 0 else ""
    elif t == "album":
        path = f"/{r.group_id}/__album__/{r.name}"
        ext = "album"
    elif t == "essence":
        path = f"/{r.group_id}/__essence__/{r.name}"
        ext = "essence"
    else:
        path = f"/{r.group_id}/{r.name}"
        ext = ""
    return path, ext


def _row_with_meta(row) -> dict:
    d = dict(row)
    if d.get("meta"):
        try:
            d["meta"] = json.loads(d["meta"])
        except ValueError:
            pass
    return d
