"""Sync domain — sync logs and snapshots."""
from __future__ import annotations

import json
import sqlite3
import time

from .state import StorePart
from typing import TYPE_CHECKING

from core.domain.sync import SyncLog, SyncResult, Snapshot

if TYPE_CHECKING:
    from .connection import ConnectionManager

# Append-only history retention: keep the newest N rows and drop anything
# older than the cutoff (both conditions must hold, so a burst of writes
# never deletes the recent window). Without this the tables grew unbounded
# across restarts.
SYNC_LOG_KEEP = 5000
SNAPSHOT_KEEP = 2000
SYNC_LOG_MAX_AGE_S = 30 * 86400
SNAPSHOT_MAX_AGE_S = 90 * 86400


class SyncMixin(StorePart):
    """Sync log and snapshot operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def create_sync_log(self, log: SyncLog) -> int:
        def _do(conn: sqlite3.Connection):
            gid = (log.group_id or "unknown") if log.group_id is not None else "unknown"
            cur = conn.execute(
                "INSERT INTO sync_logs (group_id, kind, status, start_at) VALUES (?,?,?,?)",
                (gid, log.kind.value, log.status.value, log.start_at),
            )
            cutoff = int(time.time()) - SYNC_LOG_MAX_AGE_S
            conn.execute(
                "DELETE FROM sync_logs WHERE start_at < ? AND id <= ?",
                (
                    cutoff,
                    cur.lastrowid - SYNC_LOG_KEEP,
                ),
            )
            conn.commit()
            return cur.lastrowid

        return await self._conn.exec(_do)

    async def finish_sync_log(self, log_id: int, result: SyncResult) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                """
                UPDATE sync_logs SET status=?, files_found=?, files_indexed=?,
                       complete=?, error=?, end_at=?
                WHERE id=?
                """,
                (
                    result.status.value,
                    result.files_found,
                    result.files_indexed,
                    int(result.complete),
                    result.error,
                    int(time.time()),
                    log_id,
                ),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def save_snapshot(self, snap: Snapshot) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO snapshots
                  (group_id, type, file_count, total_size, used_space,
                   total_space, detail, taken_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    snap.group_id,
                    snap.type,
                    snap.file_count,
                    snap.total_size,
                    snap.used_space,
                    snap.total_space,
                    json.dumps(snap.detail, ensure_ascii=False),
                    snap.taken_at,
                ),
            )
            cutoff = int(time.time()) - SNAPSHOT_MAX_AGE_S
            conn.execute(
                "DELETE FROM snapshots WHERE taken_at < ? AND id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM snapshots)",
                (cutoff, SNAPSHOT_KEEP),
            )
            conn.commit()

        await self._conn.exec(_do)
