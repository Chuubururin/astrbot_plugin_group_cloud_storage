"""固定间隔限速器（V1.0 内联实现，docs/04 §6）。

限速范围：OneBot 扩展 API 请求之间的最小间隔（递归采集是"每文件夹一次请求"）。
V1.1 升级为 RateLimiter 端口 + 令牌桶。
"""

from __future__ import annotations

import asyncio
import time


class IntervalLimiter:
    """全局最小间隔限制：任意两次 acquire 之间至少间隔 interval 秒。"""

    def __init__(self, interval: float = 0.5, min_interval: float = 0.1):
        self.interval = max(interval, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self, mult: float = 1.0) -> None:
        """mult=1 基础间隔；批量任务（如扫描）可传 >1 放大间隔防风控。"""
        async with self._lock:
            now = time.monotonic()
            wait = self.interval * mult - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class KeyedLimiter:
    """按账号键控限速器（v2.11）：每账号独立最小间隔，跨账号并发互不等待。"""

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

    async def acquire(self, key=None, mult: float = 1.0) -> None:
        k = key if key is not None else self.default_key
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

    def inject(self, key: str, limiter: IntervalLimiter) -> None:
        """注入既有限速器（如 bootstrap 的共享限速器 → 默认键）。"""
        self._limiters[key] = limiter
