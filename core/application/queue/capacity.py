"""Capacity use cases shared by operation orchestration."""
from __future__ import annotations

from core.application.common import compute_capacity
from core.log import logger


class CapacityMixin:
    async def refresh_capacity(self, group_id: str) -> None:
        """Compute capacity with a unified policy and persist it to the groups
        table.

        When the fs API fails, capacity_of returns None -> the write is
        skipped and the last known good values are kept, preventing 0-value
        overwrite churn.
        """
        try:
            await self.queue.acquire()
            result = await self.capacity_of(group_id)
            if result is None:
                return
            used, total, count, limit = result
            await self.store.update_group_fields(
                group_id,
                used_space=used,
                total_space=total,
                file_count=count,
                limit_count=limit,
            )
        except Exception as e:
            logger.debug(f"[group-scan] capacity refresh failed for {group_id}: {e}")

    async def capacity_of(self, group_id: str) -> tuple[int, int, int, int] | None:
        """Unified capacity policy (shared with the group scanner)."""
        return await compute_capacity(self.api, self.store, group_id)
