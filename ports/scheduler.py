"""SchedulerPort — background scheduling protocol (periodic scans, scan claims).

The application layer registers/releases scan schedules through this port and
never touches the DB tables directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable


class SchedulerPort(ABC):
    """Background scan scheduling abstraction."""

    @abstractmethod
    async def claim_scan(
        self, key: str, kind: str, group_id: str, worker_id: str, ttl_sec: int
    ) -> bool:
        """Try to acquire a scan lease (True=acquired, False=held by someone else)."""

    @abstractmethod
    async def release_scan_claim(self, key: str, worker_id: str) -> bool:
        """Release a scan lease."""

    @abstractmethod
    async def upsert_scan_schedule(
        self, group_id: str, next_scan_at: float, priority: int = 0
    ) -> None:
        """Set the next scan time and priority."""

    @abstractmethod
    async def list_due_scan_groups(self, now: float) -> list[dict]:
        """List groups due for scanning (next_scan_at <= now)."""

    @abstractmethod
    def schedule_periodic(
        self, interval_sec: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        """Register a periodic task (lifecycle managed by kernel.track_task)."""
