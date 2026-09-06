"""Archive domain — archive map operations for bridge."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .state import StorePart

if TYPE_CHECKING:
    from .connection import ConnectionManager


class ArchiveMixin(StorePart):
    """Archive map operations (bridge in/out tracking)."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def get_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT resource_id, group_id, task_id, remote_path, "
                "direction, state, updated_at "
                "FROM archive_map "
                "WHERE group_id=? AND resource_id=? AND direction=?",
                (group_id, resource_id, direction),
            ).fetchone()
            return dict(row) if row else None

        return await self._conn.exec(_do)

    async def upsert_archive_map(self, row: dict) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO archive_map "
                "(resource_id, group_id, task_id, remote_path, direction, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resource_id, group_id, direction) "
                "DO UPDATE SET task_id=?, remote_path=?, state=?, updated_at=?",
                (
                    row["resource_id"],
                    row["group_id"],
                    row.get("task_id"),
                    row["remote_path"],
                    row["direction"],
                    row["state"],
                    row["updated_at"],
                    row.get("task_id"),
                    row["remote_path"],
                    row["state"],
                    row["updated_at"],
                ),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def clear_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM archive_map "
                "WHERE group_id=? AND resource_id=? AND direction=?",
                (group_id, resource_id, direction),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def list_archive_map(
        self, *, states: tuple[str, ...], direction: str
    ) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            placeholders = ",".join("?" for _ in states)
            rows = conn.execute(
                f"SELECT resource_id, group_id, task_id, remote_path, "
                f"direction, state, updated_at "
                f"FROM archive_map "
                f"WHERE state IN ({placeholders}) AND direction=?",
                (*states, direction),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._conn.exec(_do)

    async def update_archive_state(self, row: dict, state: str) -> None:
        def _do(conn: sqlite3.Connection):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET state=?, updated_at=? "
                "WHERE resource_id=? AND group_id=? AND direction=?",
                (state, now, row["resource_id"], row["group_id"], row["direction"]),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def update_archive_remote_path(
        self, resource_id: int, group_id: str, direction: str, new_remote_path: str
    ) -> None:
        def _do(conn: sqlite3.Connection):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET remote_path=?, updated_at=? "
                "WHERE resource_id=? AND group_id=? AND direction=?",
                (new_remote_path, now, resource_id, group_id, direction),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def update_archive_state_by_task(self, task_id: str, state: str) -> None:
        def _do(conn: sqlite3.Connection):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET state=?, updated_at=? WHERE task_id=?",
                (state, now, task_id),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def get_archive_map_by_task(self, task_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT resource_id, group_id, task_id, remote_path, "
                "direction, state, updated_at "
                "FROM archive_map "
                "WHERE task_id=?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

        return await self._conn.exec(_do)

    async def list_archived_done_ids(
        self, resource_ids: list[int], direction: str = "out"
    ) -> set[int]:
        def _do(conn: sqlite3.Connection):
            if not resource_ids:
                return set()
            marks = ",".join("?" for _ in resource_ids)
            rows = conn.execute(
                f"SELECT DISTINCT resource_id FROM archive_map "
                f"WHERE resource_id IN ({marks}) AND direction=? AND state='done'",
                (*resource_ids, direction),
            ).fetchall()
            return {r[0] for r in rows}

        return await self._conn.exec(_do)
