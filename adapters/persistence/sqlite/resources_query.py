"""Resources query domain — paged search, stats, aggregates."""
from __future__ import annotations

import json
import sqlite3
import time

from .like import like_contains, like_escape
from .resources import _PART_FOLD_COND
from .state import StorePart

from core.domain.sync import Page, PageItem, ResourceQuery, ResourceStats

# "This file also lives on the netdisk" cross-reference, shared by the
# store_status=netdisk and store_status=none filters. It is a module constant
# because it is the query whose plan sqlite_stat1 decides: without planner
# statistics the correlated EXISTS is served from idx_archive_map_state (two
# distinct values) instead of the primary key's resource_id prefix, which makes
# it quadratic. tests/unit/test_sqlite_planner.py pins that plan against this
# exact string.
ARCHIVE_MAP_OUT_DONE = (
    "SELECT 1 FROM archive_map am "
    "WHERE am.resource_id = resources.id "
    "AND am.direction = 'out' AND am.state = 'done'"
)


class ResourceQueryMixin(StorePart):
    """List/search/stats over the resources table."""

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
                    where.append(f"EXISTS ({ARCHIVE_MAP_OUT_DONE})")
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
                        f"NOT EXISTS ({ARCHIVE_MAP_OUT_DONE})"
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

    async def count_active(self, group_id: str) -> int:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchone()
            return int(row[0] or 0)

        return await self._conn.exec(_do)
