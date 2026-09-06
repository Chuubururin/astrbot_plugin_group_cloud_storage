"""Groups domain — group CRUD, managed state, accounts."""
from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from .state import StorePart

from core.domain.sync import GroupInfo

if TYPE_CHECKING:
    from .connection import ConnectionManager

_GROUP_FIELD_WHITELIST = frozenset(
    {
        "display_name",
        "label",
        "sort_order",
        "last_scan_at",
        "group_name",
        "role",
        "used_space",
        "total_space",
        "file_count",
        "limit_count",
    }
)


class GroupsMixin(StorePart):
    """Group management operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def list_groups(self, include_hidden: bool = False) -> list[GroupInfo]:
        def _do(conn: sqlite3.Connection):
            sql = (
                """SELECT group_id, group_name, join_time, last_sync_at,
                          role, display_name, sort_order, label, last_scan_at,
                          used_space, total_space, file_count, limit_count, managed,
                          album_count, essence_count, account_id, hidden
                   FROM groups"""
            )
            if not include_hidden:
                sql += " WHERE hidden = 0"
            sql += " ORDER BY sort_order, group_id"
            rows = conn.execute(sql).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["sort_order"] = d.get("sort_order") or 0
                out.append(GroupInfo(**d))
            return out

        return await self._conn.exec(_do)

    async def upsert_groups(self, items: list[GroupInfo]) -> int:
        items = [g for g in items if g.group_id]

        def _do(conn: sqlite3.Connection):
            if not items:
                return 0
            now = int(time.time())
            conn.execute("BEGIN")
            try:
                n = 0
                for g in items:
                    cur = conn.execute(
                        """INSERT INTO groups
                             (group_id, group_name, role, display_name, sort_order,
                              label, join_time, last_scan_at,
                              used_space, total_space, file_count, limit_count,
                              album_count, essence_count, account_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(group_id) DO UPDATE SET
                             group_name=excluded.group_name,
                             role=excluded.role,
                             display_name=COALESCE(excluded.display_name, display_name),
                             last_scan_at=excluded.last_scan_at,
                             used_space=excluded.used_space,
                             total_space=excluded.total_space,
                             file_count=excluded.file_count,
                             limit_count=excluded.limit_count,
                             album_count=excluded.album_count,
                             essence_count=excluded.essence_count,
                             account_id=COALESCE(NULLIF(excluded.account_id, ''), groups.account_id),
                             managed=groups.managed
                        """,
                        (
                            g.group_id,
                            g.group_name,
                            g.role,
                            g.display_name,
                            g.sort_order,
                            g.label,
                            g.join_time or now,
                            g.last_scan_at or now,
                            g.used_space,
                            g.total_space,
                            g.file_count,
                            g.limit_count,
                            g.album_count,
                            g.essence_count,
                            g.account_id,
                        ),
                    )
                    n += cur.rowcount
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        return await self._conn.exec(_do)

    async def update_group_fields(self, group_id: str, **fields) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - _GROUP_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid group fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE groups SET {sets} WHERE group_id=?",
                [*fields.values(), group_id],
            )
            conn.commit()

        await self._conn.exec(_do)

    async def set_groups_managed(self, group_ids: list[str], managed: int) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                for gid in group_ids:
                    conn.execute(
                        "UPDATE groups SET managed=? WHERE group_id=?", (managed, gid)
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)

    async def reorder_groups(self, ordered_ids: list[str]) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                for i, gid in enumerate(ordered_ids):
                    conn.execute(
                        "UPDATE groups SET sort_order=? WHERE group_id=?",
                        (i + 1, gid),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)

    async def list_accounts(self) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT account_id, COUNT(*) AS groups FROM groups "
                "WHERE account_id IS NOT NULL AND account_id != '' "
                "AND managed = 1 "
                "GROUP BY account_id ORDER BY groups DESC"
            ).fetchall()
            return [
                {"account_id": r["account_id"], "groups": r["groups"]} for r in rows
            ]

        return await self._conn.exec(_do)

    async def mark_all_groups_managed(self, managed: int) -> int:
        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET managed=? WHERE managed!=?",
                (managed, managed),
            )
            conn.commit()
            return cur.rowcount

        return await self._conn.exec(_do)

    async def mark_account_groups_managed(self, account_id: str, managed: int) -> int:
        if not account_id:
            return 0

        def _do(conn: sqlite3.Connection):
            # Guard with removed=0: bringing an account back online must not
            # resurrect groups the user explicitly removed from management.
            guard = "AND removed=0" if managed == 1 else ""
            cur = conn.execute(
                f"UPDATE groups SET managed=? WHERE account_id=? AND managed!=? {guard}",
                (managed, account_id, managed),
            )
            conn.commit()
            return cur.rowcount

        await self._conn.exec(_do)

    async def restore_account_groups(self, account_id: str) -> int:
        return await self.mark_account_groups_managed(account_id, 1)

    async def mark_groups_removed(self, group_ids: list[str], removed: int) -> int:
        """Mark groups user-removed (removed=1) or restore them (removed=0)."""
        if not group_ids:
            return 0

        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                n = 0
                for gid in group_ids:
                    cur = conn.execute(
                        "UPDATE groups SET removed=? WHERE group_id=? AND removed!=?",
                        (int(removed), str(gid), int(removed)),
                    )
                    n += cur.rowcount
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        return await self._conn.exec(_do)

    async def restore_all_groups(self) -> int:
        """Startup self-heal: re-enable every group not explicitly removed by
        the user. Repairs rows left managed=0 by historical offline-detection
        poisoning; the periodic liveness sweep re-hides genuinely offline
        accounts afterwards."""
        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET managed=1 WHERE removed=0 AND managed!=1"
            )
            conn.commit()
            return cur.rowcount

        return await self._conn.exec(_do)

    async def list_account_group_ids(self, account_id: str) -> list[str]:
        if not account_id:
            return []

        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT group_id FROM groups WHERE account_id=?", (str(account_id),)
            ).fetchall()
            return [str(r[0]) for r in rows]

        return await self._conn.exec(_do)

    async def hide_account_groups(self, account_id: str, hidden: int) -> int:
        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET hidden=? WHERE account_id=?", (int(hidden), account_id)
            )
            conn.commit()
            return cur.rowcount

        return await self._conn.exec(_do)
