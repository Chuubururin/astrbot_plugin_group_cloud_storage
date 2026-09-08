"""RateLimiter 端口协议一致性补测（①重构后的安全网）。

所有实现必须满足 ports.limiter.RateLimiter 结构契约：
- runtime_checkable isinstance 通过
- await acquire(mult=…, account=…) 阻塞式语义
- keys() 状态查询面（KeyedLimiter/NullLimiter）
"""

from __future__ import annotations

import time

import pytest

from adapters.limiter.interval import IntervalLimiter, KeyedLimiter
from ports import NullLimiter, RateLimiter


@pytest.mark.parametrize(
    "impl",
    [NullLimiter(), IntervalLimiter(0.01), KeyedLimiter(0.01)],
    ids=["null", "interval", "keyed"],
)
def test_structurally_conforms_to_port(impl):
    """结构性契约：runtime_checkable Protocol isinstance。"""
    assert isinstance(impl, RateLimiter)


@pytest.mark.asyncio
async def test_null_limiter_is_noop():
    nl = NullLimiter()
    await nl.acquire()                      # 默认参数
    await nl.acquire(mult=100.0, account="a")  # 任意参数即刻返回
    assert nl.keys() == []


@pytest.mark.asyncio
async def test_keyed_limiter_mult_amplifies_interval():
    """mult>1 放大同账号间隔（批量任务防风控语义）。"""
    lim = KeyedLimiter(0.05)
    await lim.acquire(account="A")
    t0 = time.monotonic()
    await lim.acquire(mult=3.0, account="A")
    assert time.monotonic() - t0 >= 0.14  # 3 × 0.05


@pytest.mark.asyncio
async def test_interval_limiter_default_account_serializes():
    """IntervalLimiter 单例节奏：连续 acquire 至少间隔 interval。"""
    lim = IntervalLimiter(0.06)
    await lim.acquire()
    t0 = time.monotonic()
    await lim.acquire()
    assert time.monotonic() - t0 >= 0.045


@pytest.mark.asyncio
async def test_keyed_limiter_keys_reflects_activity():
    lim = KeyedLimiter(0.01)
    await lim.acquire(account="A")
    await lim.acquire(account="B")
    assert sorted(lim.keys()) == ["A", "B"]
