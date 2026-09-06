from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from .state import StorePart


class SchedulingMixin(StorePart):
    """Due-entry scheduling over scan_schedule (TTL-based group rescans)
    plus scan_claims leases (multi-instance reservation)."""

    if TYPE_CHECKING:
        from .connection import ConnectionManager

        _conn: "ConnectionManager"

    async def upsert_scan_schedule(self, group_id, next_scan_at, priority=0):
        def f(c): c.execute("INSERT INTO scan_schedule(group_id,next_scan_at,priority) VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET next_scan_at=excluded.next_scan_at, priority=excluded.priority",(str(group_id),int(next_scan_at),int(priority))); c.commit()
        return await self._conn.exec(f)
    async def list_due_scan_groups(self, now=None, limit=100):
        def f(c): return [dict(r) for r in c.execute("SELECT * FROM scan_schedule WHERE next_scan_at<=? ORDER BY priority DESC,next_scan_at LIMIT ?",(int(now or time.time()),int(limit))).fetchall()]
        return await self._conn.exec(f)
    async def claim_scan(self, claim_key, kind, group_id, worker_id, lease_seconds=300):
        def f(c):
            now=int(time.time()); c.execute("DELETE FROM scan_claims WHERE lease_until<=?",(now,)); cur=c.execute("INSERT OR IGNORE INTO scan_claims VALUES(?,?,?,?,?,?)",(claim_key,kind,group_id,worker_id,now,now+int(lease_seconds))); c.commit(); return cur.rowcount==1
        return await self._conn.exec(f)
    async def release_scan_claim(self, claim_key, worker_id):
        def f(c): cur=c.execute("DELETE FROM scan_claims WHERE claim_key=? AND worker_id=?",(claim_key,worker_id)); c.commit(); return cur.rowcount==1
        return await self._conn.exec(f)
