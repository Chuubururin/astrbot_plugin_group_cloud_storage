"""Volumes domain — volume CRUD and cross-group storage."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .state import StorePart

from core.domain.sync import VolumeInfo

if TYPE_CHECKING:
    from .connection import ConnectionManager

_VOLUME_FIELD_WHITELIST = frozenset(
    {"source_ref", "busid", "sha256", "status", "part_name", "size"}
)


class VolumesMixin(StorePart):
    """Volume management operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def insert_volumes(self, items: list[VolumeInfo]) -> None:
        def _do(conn: sqlite3.Connection):
            if not items:
                return
            try:
                conn.executemany(
                    """INSERT INTO volumes
                         (parent_resource_id, seq, part_name, source_ref,
                          busid, size, sha256, status, upload_time, group_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(parent_resource_id, seq) DO UPDATE SET
                         part_name=excluded.part_name, status=excluded.status,
                         size=excluded.size, sha256=excluded.sha256,
                         group_id=COALESCE(excluded.group_id, group_id)
                    """,
                    [
                        (
                            v.parent_resource_id, v.seq, v.part_name, v.source_ref,
                            v.busid, v.size, v.sha256, v.status, v.upload_time,
                            v.group_id,
                        )
                        for v in items
                    ],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)

    async def list_volumes(self, parent_resource_id: str) -> list[VolumeInfo]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT parent_resource_id, seq, part_name, source_ref, busid, "
                "size, sha256, status, upload_time, group_id FROM volumes "
                "WHERE parent_resource_id=? ORDER BY seq",
                (parent_resource_id,),
            ).fetchall()
            return [VolumeInfo(**dict(r)) for r in rows]

        return await self._conn.exec(_do)

    async def update_volume_fields(
        self, parent_resource_id: str, seq: int, **fields
    ) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - _VOLUME_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid volume fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE volumes SET {sets} WHERE parent_resource_id=? AND seq=?",
                [*fields.values(), parent_resource_id, seq],
            )
            conn.commit()

        await self._conn.exec(_do)

    async def backfill_volume_by_part(
        self, group_id: str, part_name: str, source_ref: str, busid: int
    ) -> int:
        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                """UPDATE volumes SET source_ref=?, busid=?
                   WHERE part_name=?
                     AND (source_ref IS NULL OR source_ref='')
                     AND parent_resource_id LIKE ?
                """,
                (source_ref, busid, part_name, f"{group_id}:file:%"),
            )
            conn.commit()
            return cur.rowcount

        return await self._conn.exec(_do)

    async def remove_volumes(self, parent_resource_id: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM volumes WHERE parent_resource_id=?", (parent_resource_id,)
            )
            conn.commit()

        await self._conn.exec(_do)

    async def has_volume_part(self, group_id: str, part_name_glob: str) -> bool:
        """True when the group already has a volume part matching the glob.

        Identity guard for re-conversion after meta loss (2026-09-09 live:
        meta was lost after file_id churn, parts stayed attached to the stale
        parent, and a second convert duplicated uploads). The pattern is
        built in plugin code from an escaped filename stem and a fixed
        suffix, then bound as a parameter.

        Scoping uses the parent_resource_id prefix (`<group>:file:%`) rather
        than volumes.group_id: the convert path never populates that column,
        so every live row carries NULL there (verified 2026-09-10).
        """
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT 1 FROM volumes WHERE parent_resource_id LIKE ? "
                "AND part_name LIKE ? ESCAPE '\\' LIMIT 1",
                (f"{group_id}:file:%", part_name_glob),
            ).fetchone()
            return row is not None

        return await self._conn.exec(_do)
