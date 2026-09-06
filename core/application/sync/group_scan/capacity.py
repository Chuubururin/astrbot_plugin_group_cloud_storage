from __future__ import annotations



class CapacityMixin:
    """Capacity calculation and refresh methods."""

    async def _capacity_of(self, group_id: str, api=None) -> tuple[int, int, int, int] | None:
        """Single capacity standard (cloud first, local index fallback).

        fs success with total>0 -> returns the 4-tuple.
        fs failure or total=0 -> returns None (the caller skips the capacity
        write and keeps the previous values).
        """
        _api = api or self.api
        try:
            fs = await _api.get_group_fs_info(group_id)
        except Exception:
            return None
        if not fs.total_space:
            return None
        used = fs.used_space or await self.store.sum_resource_sizes(group_id)
        count = fs.file_count or await self.store.count_active(group_id)
        return used, fs.total_space, count, fs.limit_count
