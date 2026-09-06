"""Outbox domain — task ledger and operation flow."""
from __future__ import annotations

import json
import sqlite3
import time

from .state import StorePart
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connection import ConnectionManager

LEDGER_BREAKPOINT_KINDS = ("convert_volumes", "video_upload", "netdisk_index")


def _now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class OutboxMixin(StorePart):
    """Task ledger and operation flow operations."""

    if TYPE_CHECKING:
        _conn: "ConnectionManager"

    async def ledger_upsert(
        self,
        task_id: str,
        kind: str,
        target: str = "",
        payload: dict | None = None,
        state: str = "pending",
        retries: int = 0,
        error: str | None = None,
    ) -> None:
        def _do(conn: sqlite3.Connection):
            now = _now_ts()
            conn.execute(
                """INSERT INTO op_ledger
                   (task_id, kind, target, payload, state, retries, error,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     state=excluded.state,
                     retries=excluded.retries,
                     error=CASE WHEN excluded.error IS NOT NULL THEN excluded.error
                                WHEN excluded.state IN ('done','cancelled') THEN NULL
                                ELSE op_ledger.error END,
                     updated_at=excluded.updated_at""",
                (
                    task_id,
                    kind,
                    target,
                    json.dumps(payload or {}, ensure_ascii=False),
                    state,
                    int(retries or 0),
                    error,
                    now,
                    now,
                ),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def ledger_get(self, task_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM op_ledger WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["payload"] = json.loads(d.get("payload") or "{}")
            return d

        return await self._conn.exec(_do)

    async def ledger_query(
        self,
        state: str | None = None,
        kind: str | None = None,
        target: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            sql = "SELECT * FROM op_ledger WHERE 1=1"
            args: list = []
            if state:
                sql += " AND state=?"
                args.append(state)
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            if target:
                sql += " AND target=?"
                args.append(target)
            sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
            out = []
            for r in conn.execute(sql, args).fetchall():
                d = dict(r)
                d["payload"] = json.loads(d.get("payload") or "{}")
                out.append(d)
            return out

        return await self._conn.exec(_do)

    async def ledger_reconcile(self) -> int:
        def _do(conn: sqlite3.Connection):
            now = _now_ts()
            n1 = conn.execute(
                """UPDATE op_ledger SET state='pending', updated_at=?
                   WHERE state IN ('running','paused','retry')
                     AND kind IN (?,?,?)""",
                (now,) + LEDGER_BREAKPOINT_KINDS,
            ).rowcount
            n2 = conn.execute(
                """UPDATE op_ledger SET state='failed',
                   error=COALESCE(error, 'interrupted by restart'),
                   updated_at=?
                   WHERE state IN ('running','paused','retry','pending')
                     AND kind NOT IN (?,?,?)""",
                (now,) + LEDGER_BREAKPOINT_KINDS,
            ).rowcount
            conn.commit()
            return n1 + n2

        return await self._conn.exec(_do)

    async def ops_append(
        self, task_id: str, action: str, before: dict | None, after: dict | None
    ) -> None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM op_ops WHERE task_id=?",
                (task_id,),
            ).fetchone()
            seq = row["s"] if row else 1
            conn.execute(
                """INSERT INTO op_ops (task_id, seq, action, before, after, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    task_id,
                    seq,
                    action,
                    json.dumps(before or {}, ensure_ascii=False),
                    json.dumps(after or {}, ensure_ascii=False),
                    _now_ts(),
                ),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def ops_list(self, task_id: str) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM op_ops WHERE task_id=? ORDER BY seq, op_id",
                (task_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["before"] = json.loads(d.get("before") or "{}")
                d["after"] = json.loads(d.get("after") or "{}")
                out.append(d)
            return out

        return await self._conn.exec(_do)

    async def ops_last_for_resource(self, action: str, resource_id: int) -> dict | None:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM op_ops WHERE action=? AND task_id='' "
                "ORDER BY op_id DESC LIMIT 200",
                (action,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["before"] = json.loads(d.get("before") or "{}")
                d["after"] = json.loads(d.get("after") or "{}")
                if d["after"].get("id") == resource_id:
                    return d
            return None

        return await self._conn.exec(_do)
