"""Search domain — FTS5 matching and tag cloud."""
from __future__ import annotations

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
                # BUG-23: escape FTS5 special characters to prevent query
                # semantics from changing (e.g. "test*" becoming prefix match,
                # "NOT secret" becoming exclusion). Only word characters and
                # CJK are kept; special chars are stripped.
                def _fts_escape(term: str) -> str:
                    cleaned = re.sub(r'[^\w\u4e00-\u9fff]', '', term)
                    return cleaned if cleaned else term
                expr = " AND ".join(
                    '"%s"' % _fts_escape(t) for t in long_t if _fts_escape(t)
                )
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
            # json_each expands the tags JSON array inside SQLite: one
            # aggregate query instead of loading every row's tags into
            # Python. The inner subquery filters to json_valid rows before
            # json_each runs (a table-valued function over a malformed value
            # raises, and SQLite does not guarantee WHERE-before-join order).
            inner = (
                "SELECT tags FROM resources"
                "  WHERE status='active' AND tags IS NOT NULL"
                "    AND tags != '' AND json_valid(tags)"
            )
            params: list = []
            if kind:
                inner += " AND type=?"
                params.append(kind)
            sql = (
                "SELECT j.value AS tag, COUNT(*) AS cnt FROM ("
                + inner
                + ") r, json_each(r.tags) j "
                "WHERE j.type IN ('text','string')"
                " GROUP BY j.value ORDER BY cnt DESC, tag ASC"
            )
            rows = conn.execute(sql, params).fetchall()
            result = [{"tag": r[0], "count": r[1]} for r in rows]
            # BUG-26: bound cache size — evict oldest entries when exceeding
            # 16 keys (generous for the ~3 expected keys; guards against
            # unbounded growth if kind is ever user-controlled).
            if len(self._tag_cloud_cache) >= 16:
                oldest_key = min(self._tag_cloud_cache, key=lambda k: self._tag_cloud_cache[k][0])
                self._tag_cloud_cache.pop(oldest_key, None)
            self._tag_cloud_cache[key] = (now, result)
            return result

        return await self._conn.exec(_do)
