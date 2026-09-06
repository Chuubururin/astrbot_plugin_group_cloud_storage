"""Search domain — FTS5 matching and tag cloud."""
from __future__ import annotations

import json
import re
import sqlite3

from .state import StorePart
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connection import ConnectionManager


class SearchMixin(StorePart):
    """Full-text search and tag cloud operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"
        _tag_cloud_cache: dict

    async def fts_match(
        self, group_id: str | None, q: str, limit: int = 2000
    ) -> list[int]:
        def _do(conn: sqlite3.Connection):
            qs = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", (q or "").lower()))
            if not qs:
                return []
            terms = qs.split()
            long_t = [t for t in terms if len(t) >= 3]
            short_t = [t for t in terms if len(t) < 3]
            expr: str | None = None
            if long_t:
                expr = " AND ".join('"%s"' % t.replace('"', '""') for t in long_t)
            like_sql = ""
            like_params: list = []
            if short_t:
                conds = []
                for t in short_t:
                    esc = (
                        t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    )
                    conds.append(
                        "(lower(name) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(json_extract(meta, '$.summary'), '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(tags, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(path, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(folder_name, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(mime, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(uploader_name, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(uploader_id, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(sha256, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(source_ref, '')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(group_id, '')) LIKE ? ESCAPE '\\')"
                    )
                    like_params.extend([f"%{esc}%"] * 11)
                like_sql = " AND " + " AND ".join(conds)
            if expr is not None:
                # Join directly against the FTS table so SQLite can stream rowids
                # without materializing a large Python-side IN list.
                sql = (
                "SELECT r.id FROM resources AS r "
                "WHERE r.status='active' "
                "AND r.id IN (SELECT rowid FROM resources_fts "
                "WHERE resources_fts MATCH ?)"
                    + like_sql
                    + (" AND r.group_id=?" if group_id else "")
                    + f" LIMIT {int(limit)}"
                )
                params = [expr] + like_params + ([str(group_id)] if group_id else [])
                return [r[0] for r in conn.execute(sql, params)]
            sql = (
                "SELECT id FROM resources WHERE status='active'"
                + like_sql
                + (" AND group_id=?" if group_id else "")
                + f" LIMIT {int(limit)}"
            )
            params = like_params + ([str(group_id)] if group_id else [])
            return [r[0] for r in conn.execute(sql, params)]

        return await self._conn.exec(_do)

    async def tag_cloud(self, kind: str | None = None) -> list[dict]:
        now = time.monotonic()
        key = kind or "*"
        cache = self._tag_cloud_cache.get(key)
        if cache and now - cache[0] < 120.0:
            return cache[1]

        def _do(conn: sqlite3.Connection):
            sql = (
                "SELECT tags FROM resources WHERE status='active' "
                "AND tags IS NOT NULL AND tags != '' AND tags != '[]'"
            )
            params: list = []
            if kind:
                sql += " AND type=?"
                params.append(kind)
            rows = conn.execute(sql, params).fetchall()
            counts: dict[str, int] = {}
            for r in rows:
                try:
                    for t in json.loads(r[0] or "[]"):
                        counts[t] = counts.get(t, 0) + 1
                except ValueError:
                    continue
            result = [
                {"tag": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ]
            self._tag_cloud_cache[key] = (now, result)
            return result

        return await self._conn.exec(_do)
