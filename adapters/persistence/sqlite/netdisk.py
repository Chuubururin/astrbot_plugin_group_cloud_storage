"""Netdisk domain — netdisk metadata and indexing."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .state import StorePart

if TYPE_CHECKING:
    from .connection import ConnectionManager


class NetdiskMixin(StorePart):
    """Netdisk metadata operations ."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def upsert_netdisk_rows(self, rows: list[dict]) -> int:
        def _do(conn: sqlite3.Connection):
            cur = conn.cursor()
            before = conn.total_changes
            for r in rows:
                cur.execute(
                    "INSERT OR IGNORE INTO netdisk_meta "
                    "(remote_path, name, is_dir, size, type, tags, registered_at, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["remote_path"],
                        r["name"],
                        int(r.get("is_dir") or 0),
                        int(r.get("size") or 0),
                        r.get("type") or "other",
                        r.get("tags") or "",
                        r["registered_at"],
                        r.get("indexed_at") or "",
                    ),
                )
            conn.commit()
            return conn.total_changes - before

        return await self._conn.exec(_do)

    async def get_netdisk_meta(self, dir_prefix: str) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT remote_path, name, is_dir, size, type, tags, "
                "registered_at, indexed_at "
                "FROM netdisk_meta WHERE remote_path LIKE ?",
                (dir_prefix + "%",),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._conn.exec(_do)

    async def set_netdisk_tags(self, remote_path: str, tags: str) -> None:
        def _do(conn: sqlite3.Connection):
            now = datetime.now(timezone.utc).isoformat()
            # Upsert, not blind UPDATE: the UI may tag a path that browse/deep
            # index never registered (a plain UPDATE would silently hit 0 rows
            # and the tag would be lost on the next registration). The
            # placeholder row carries only the key; name/size/type stay
            # inert because browse and list responses always take them from
            # the live OpenList listing.
            cur = conn.execute(
                "INSERT INTO netdisk_meta "
                "(remote_path, name, is_dir, size, type, tags, registered_at, indexed_at) "
                "VALUES (?, ?, 0, 0, 'other', ?, ?, '') "
                "ON CONFLICT(remote_path) DO UPDATE SET tags=excluded.tags",
                (remote_path, remote_path.rsplit("/", 1)[-1] or remote_path, tags, now),
            )
            conn.commit()
            return cur.rowcount

        await self._conn.exec(_do)

    async def mark_netdisk_indexed(self, remote_paths: list[str]) -> None:
        def _do(conn: sqlite3.Connection):
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "UPDATE netdisk_meta SET indexed_at=? WHERE remote_path=?",
                [(now, p) for p in remote_paths],
            )
            conn.commit()

        await self._conn.exec(_do)

    async def search_netdisk_meta(self, keyword: str, dir_prefix: str | None = None) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            if dir_prefix:
                rows = conn.execute(
                    "SELECT remote_path, name, is_dir, size, type, tags, "
                    "registered_at, indexed_at "
                    "FROM netdisk_meta WHERE (name LIKE ? OR tags LIKE ?) "
                    "AND remote_path LIKE ?",
                    (f"%{keyword}%", f"%{keyword}%", dir_prefix + "%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT remote_path, name, is_dir, size, type, tags, "
                    "registered_at, indexed_at "
                    "FROM netdisk_meta WHERE name LIKE ? OR tags LIKE ?",
                    (f"%{keyword}%", f"%{keyword}%"),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._conn.exec(_do)
