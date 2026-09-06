"""Fixed-interval rate limiter.

Scope: enforces a minimum interval between OneBot extension API requests
(recursive collection issues one request per folder). Provided as the
fixed-interval implementation behind the RateLimiter port
(ports/limiter.py).
"""

from __future__ import annotations

import asyncio
import time


class IntervalLimiter:
    """Global minimum-interval limit: at least `interval` seconds elapse
    between any two acquire() calls."""

    def __init__(self, interval: float = 0.5, min_interval: float = 0.1):
        self.interval = max(interval, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(
        self, mult: float = 1.0, account: str | None = None
    ) -> None:
        """mult=1 uses the base interval; batch jobs (e.g. scans) may pass
        mult > 1 to widen the interval and reduce risk-control pressure.

        account exists only to satisfy the RateLimiter port signature
        (this global limiter does not distinguish accounts).
        """
        async with self._lock:
            now = time.monotonic()
            wait = self.interval * mult - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class KeyedLimiter:
    """Per-account keyed rate limiter: each account gets its own minimum
    interval, and concurrent acquires on different accounts never wait
    for each other."""

    def __init__(
        self,
        interval: float = 0.5,
        min_interval: float = 0.1,
        default_key: str = "__global__",
    ):
        self.interval = interval
        self.min_interval = min_interval
        self.default_key = default_key
        self._limiters: dict = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, mult: float = 1.0, account: str | None = None
    ) -> None:
        k = account if account is not None else self.default_key
        lim = self._limiters.get(k)
        if lim is None:
            async with self._lock:
                lim = self._limiters.get(k)
                if lim is None:
                    lim = IntervalLimiter(self.interval, self.min_interval)
                    self._limiters[k] = lim
        await lim.acquire(mult)

    def keys(self) -> list:
        return list(self._limiters)
