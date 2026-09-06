"""RateLimiter port — the single exit point for rate limiting policy.

The application layer depends only on this port; concrete implementations
(IntervalLimiter/KeyedLimiter) live in adapters/limiter and are injected by
the assembly layer (bootstrap/runtime).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    """Blocking rate limiter port: ``acquire`` returns only after quota is granted."""

    async def acquire(
        self, mult: float = 1.0, account: str | None = None
    ) -> None:
        """mult=1 base interval; >1 widens interval for batch jobs; account keys concurrency."""
        ...


class NullLimiter:
    """No-op limiter: no rate limiting.

    Default when OpQueue has no limiter injected (tests / minimal deploys).
    """

    async def acquire(
        self, mult: float = 1.0, account: str | None = None
    ) -> None:
        return None

    def keys(self) -> list:
        """State query surface aligned with KeyedLimiter (no accounts = empty list)."""
        return []


__all__ = ["RateLimiter", "NullLimiter"]
