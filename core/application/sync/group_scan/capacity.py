"""Scan-side capacity policy (delegates to the shared implementation)."""
from __future__ import annotations

from core.application.common import compute_capacity


class CapacityMixin:
    """Capacity calculation and refresh methods."""

    async def _capacity_of(self, group_id: str, api=None) -> tuple[int, int, int, int] | None:
        """Single capacity standard (cloud first, local index fallback).

        fs success with total>0 -> returns the 4-tuple.
        fs failure or total=0 -> returns None (the caller skips the capacity
        write and keeps the previous values).
        """
        return await compute_capacity(api or self.api, self.store, group_id)
