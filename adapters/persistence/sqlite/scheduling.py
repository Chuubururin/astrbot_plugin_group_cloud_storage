from __future__ import annotations

import time

from .state import StorePart


class SchedulingMixin(StorePart):
    """Due-entry scheduling over scan_schedule (TTL-based group rescans)."""

    async def upsert_scan_schedule(self, group_id, next_scan_at, priority=0):
        def f(c):
            c.execute("INSERT INTO scan_schedule(group_id,next_scan_at,priority) VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET next_scan_at=excluded.next_scan_at, priority=excluded.priority",(str(group_id),int(next_scan_at),int(priority)))
            c.commit()
        return await self._conn.exec(f)
    async def list_due_scan_groups(self, now=None, limit=100):
        def f(c): return [dict(r) for r in c.execute("SELECT * FROM scan_schedule WHERE next_scan_at<=? ORDER BY priority DESC,next_scan_at LIMIT ?",(int(now or time.time()),int(limit))).fetchall()]
        return await self._conn.exec(f)
