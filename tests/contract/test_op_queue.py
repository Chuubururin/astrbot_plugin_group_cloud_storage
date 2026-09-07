"""OpQueue 限速队列测试（docs/09 §12.5）：限速、重试、SSE、取消。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.queue import Op, OpCancelError, OpQueue  # noqa: E402


class _Recorder:
    """记录 handler 调用时间/次数，可注入失败。"""

    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self.times: list[float] = []
        self.fails = 0

    async def run(self, op: Op) -> None:
        self.times.append(time.monotonic())
        if self.fails < self.fail_first:
            self.fails += 1
            raise RuntimeError("boom")


async def _drain(queue: OpQueue, n: int, timeout: float = 8.0) -> None:
    """等待 n 个 op 全部完成（recent 中出现对应条数）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        if len(st["recent"]) >= n and not st["running"] and st["depth"] == 0:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"queue drain timeout: {await queue.status()}")


@pytest.mark.asyncio
async def test_rate_limited_execution():
    from adapters.limiter.interval import KeyedLimiter

    rec = _Recorder()
    # 限速经 RateLimiter 端口注入（bootstrap 组装 KeyedLimiter）；interval 参数仅为兼容保留
    q = OpQueue(rec.run, interval=0.0, limiter=KeyedLimiter(interval=0.06))
    await q.start()
    for _ in range(3):
        await q.submit("test")
    await _drain(q, 3)
    assert len(rec.times) == 3
    # 相邻执行间隔 ≥ interval（容差 15ms）
    gaps = [b - a for a, b in zip(rec.times, rec.times[1:])]
    assert all(g >= 0.045 for g in gaps), gaps
    await q.shutdown()


@pytest.mark.asyncio
async def test_retry_with_backoff():
    rec = _Recorder(fail_first=2)
    q = OpQueue(rec.run, interval=0.0, max_retries=3, backoff_base=0.05)
    await q.start()
    await q.submit("test")
    await _drain(q, 1)
    assert rec.fails == 2  # 前两次失败，第三次成功（重试次数会进队列）
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_permanent_failure_marked():
    rec = _Recorder(fail_first=99)
    q = OpQueue(rec.run, interval=0.0, max_retries=2, backoff_base=0.03)
    await q.start()
    await q.submit("test")
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"
    assert rec.fails == 3  # 初始 + 2 次重试（上限）
    await q.shutdown()


@pytest.mark.asyncio
async def test_sse_events_flow():
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.0)
    await q.start()
    events: list[str] = []

    async def listener():
        async for ev in q.subscribe():
            events.append(ev["type"])
            if ev["type"] == "done":
                return

    t = asyncio.create_task(listener())
    await asyncio.sleep(0.05)
    await q.submit("test")
    await t
    assert events == ["queued", "started", "done"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_local_error_not_retried():
    """LOCAL_ERROR（如缺 bot 上下文）属环境态：直接失败，不做指数重试。"""
    from core.domain.enums import OneBotApiError, OneBotErrorKind

    async def fail(op):
        raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, "scan", "no bot")

    q = OpQueue(fail, interval=0.0, max_retries=5, backoff_base=0.02)
    await q.start()
    await q.submit("scan")
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"          # 未重试、直接失败
    assert "local_error" in st["recent"][0]["error"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_skips_queued_op():
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.2)  # 限速让第 2 个任务留在队列
    await q.start()
    tid1 = await q.submit("first")
    tid2 = await q.submit("second")
    # second 已出队进入限速等待（running）→ 取消位生效，等待后跳过执行
    assert q.cancel_task(tid2) is True
    await _drain(q, 2)
    assert len(rec.times) == 1  # 仅 first 执行（second 在限速等待中被取消）
    await q.shutdown()


async def _blocking_handler(gate: asyncio.Event):
    async def run(op: Op) -> None:
        await gate.wait()
    return run


@pytest.mark.asyncio
async def test_custom_high_priority_set():
    """自定义 high_priority：集合内 kind 走高优队列，集合外走常规队列。"""
    gate = asyncio.Event()
    q = OpQueue(await _blocking_handler(gate), interval=0.0,
                high_priority={"hi_kind"})
    await q.start()
    await q.submit("hi_kind")   # 高优 worker 阻塞
    await q.submit("lo_kind")   # 常规 worker 阻塞
    await asyncio.sleep(0.05)
    await q.submit("hi_kind")
    await q.submit("lo_kind")
    st = await q.status()
    assert st["high"] == 1      # 第二个 hi_kind 在高优队列
    assert st["depth"] == 2
    assert st["high_priority_kinds"] == ["hi_kind"]
    gate.set()
    await _drain(q, 4)
    await q.shutdown()


@pytest.mark.asyncio
async def test_default_high_priority_behavior_unchanged():
    """默认集合：rename 走高优，scan 走常规（内置行为不因配置化而变化）。"""
    gate = asyncio.Event()
    q = OpQueue(await _blocking_handler(gate), interval=0.0)
    await q.start()
    await q.submit("rename")    # 高优 worker 阻塞
    await q.submit("scan")      # 常规 worker 阻塞
    await asyncio.sleep(0.05)
    await q.submit("scan")
    st = await q.status()
    assert st["high"] == 0      # 第二个 scan 在常规队列
    assert st["depth"] == 1
    gate.set()
    await _drain(q, 3)
    await q.shutdown()

@pytest.mark.asyncio
async def test_keyed_limiter_accounts_parallel():
    """v2.11：键控限速——不同账号并行不受彼此节奏阻塞；同账号串行。"""
    from adapters.limiter.interval import KeyedLimiter

    lim = KeyedLimiter(interval=0.2)
    t0 = time.monotonic()
    await asyncio.gather(lim.acquire(account="A"), lim.acquire(account="B"))
    # 双账号各拿一次：几乎同时（不互等 0.2s）
    assert time.monotonic() - t0 < 0.12
    # 同账号连续两次需间隔 ≥ interval
    t0 = time.monotonic()
    await lim.acquire(account="A")
    await lim.acquire(account="A")
    assert time.monotonic() - t0 >= 0.18


@pytest.mark.asyncio
async def test_queue_per_account_concurrency():
    """v2.11：队列按账号并发消费——A/B 账号 ops 同时执行（非全局串行）。"""
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.3, slots=4)
    await q.start()
    await q.submit("t1", account="A")
    await q.submit("t2", account="B")
    await _drain(q, 2)
    # 两账号并行：第二个 op 不等待 0.3s 全局限速
    assert len(rec.times) == 2
    assert abs(rec.times[0] - rec.times[1]) < 0.25
    await q.shutdown()


@pytest.mark.asyncio
async def test_bulk_kinds_skip_pacing():
    """v2.11：重活分流——非交互重活不占全局限速节奏。"""
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.4, slots=4)
    await q.start()
    await q.submit("convert_volumes")
    await q.submit("t2")
    await _drain(q, 2)
    # convert_volumes 为 BULK：两者均立即执行（无 0.4s 串行等待）
    assert len(rec.times) == 2
    assert abs(rec.times[0] - rec.times[1]) < 0.2
    await q.shutdown()
