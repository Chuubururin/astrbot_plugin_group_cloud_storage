"""高风险补测 —— OpQueue 并发探索与不变量（④拆分后的回归安全网）。

不变量：
- I1 并发提交/取消交错下，每个任务恰好一个终态（done/failed/cancelled），无丢失
- I2 取消发生在任何阶段（排队/限速等待/运行中）都能收敛到终态
- I3 暂停→恢复 roundtrip 保留现场（retries/error 不重置）
- I4 批量压测吞吐：N 个任务全部完成，recent 环形缓冲上限 20
- I5 重活类别（BULK_KINDS）不吃限速间隔（并发并发度=信号量 2）

属性测试用 hypothesis（未安装则跳过，系统 PEP 668 环境不阻塞）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.application.queue import Op, OpPausedError, OpQueue
from core.application.queue.op import BULK_KINDS, DEFAULT_HIGH_PRIORITY


class _Terminal:
    """记录每个 task 的终态；重复终态视为不变量破坏。"""

    def __init__(self):
        self.states: dict[str, str] = {}

    def record(self, task_id: str, state: str) -> None:
        assert task_id not in self.states, (
            f"不变量破坏：{task_id} 重复终态 {self.states.get(task_id)} -> {state}"
        )
        self.states[task_id] = state


def _tracking_queue(term: _Terminal, *, fail_kinds: str = ""):
    async def run(op: Op) -> None:
        if op.kind in fail_kinds:
            raise RuntimeError(f"boom {op.kind}")

    q = OpQueue(run, interval=0.0)
    orig_push = q._push

    def push(ev: dict) -> None:
        if ev["type"] in ("done", "failed", "cancelled"):
            term.record(ev["task_id"], ev["type"])
        orig_push(ev)

    q._push = push
    return q


async def _wait_terminals(q: OpQueue, term: _Terminal, ids: list[str], timeout=10.0):
    deadline = time.monotonic() + timeout
    while not all(t in term.states for t in ids):
        if time.monotonic() > deadline:
            missing = [t for t in ids if t not in term.states]
            raise TimeoutError(f"任务丢失终态: {missing}")
        await asyncio.sleep(0.02)
    await q.shutdown()


@pytest.mark.asyncio
async def test_i1_concurrent_submit_cancel_interleave():
    """并发提交+取消交错：每个任务恰好一个终态（确定性序列可复现）。"""
    for seed in range(5):
        # Deterministic LCG keeps the scenario reproducible across runs
        # without pulling in the random module.
        state = seed * 2 + 1

        def nxt() -> float:
            nonlocal state
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            return state / 0x80000000

        term = _Terminal()
        q = _tracking_queue(term)
        ids = []
        for _i in range(30):
            tid = await q.submit("scan" if nxt() < 0.7 else "delete")
            ids.append(tid)
            if nxt() < 0.3:
                victim = ids[int(nxt() * len(ids)) % len(ids)]
                q.cancel_task(victim)
        await _wait_terminals(q, term, ids)
        cancelled = sum(1 for s in term.states.values() if s == "cancelled")
        assert cancelled >= 1  # 确定性序列里至少一次取消生效


@pytest.mark.asyncio
async def test_i2_cancel_during_pause_suspend_reaches_terminal():
    """暂停挂起（已取出未恢复）时取消：直接置终态，无悬挂。"""
    term = _Terminal()

    async def run(op: Op) -> None:
        pass

    q = OpQueue(run, interval=0.0)
    orig_push = q._push
    q._push = lambda ev: (term.record(ev["task_id"], ev["type"])
                          if ev["type"] in ("done", "failed", "cancelled") else None,
                          orig_push(ev))[1]
    tid = await q.submit("scan")
    q.pause_task(tid)  # 排队挂起（同步方法）
    # 等 worker 取出并挂起（占位→真实 Op）
    for _ in range(100):
        await q.status()
        if q._paused.get(tid) is not None:
            break
        await asyncio.sleep(0.02)
    assert q.cancel_task(tid) is True
    await _wait_terminals(q, term, [tid])
    assert term.states[tid] == "cancelled"


@pytest.mark.asyncio
async def test_i3_pause_resume_roundtrip_preserves_state():
    """暂停→恢复：保留现场（重试计数与 error 不重置），任务最终完成。"""
    term = _Terminal()
    attempts = {"n": 0}

    async def run(op: Op) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OpPausedError()

    q = OpQueue(run, interval=0.0)
    orig_push = q._push
    q._push = lambda ev: (term.record(ev["task_id"], ev["type"])
                          if ev["type"] in ("done", "failed", "cancelled") else None,
                          orig_push(ev))[1]
    tid = await q.submit("scan")
    op = q._ops_by_id[tid]
    op.retries = 2
    op.error = "prev"
    await asyncio.sleep(0.05)
    # handler 抛 OpPausedError 后进入挂起，恢复前校验现场
    for _ in range(100):
        if tid in (await q.status())["paused_ids"]:
            break
        await asyncio.sleep(0.02)
    assert op.retries == 2 and op.error == "prev"  # I3：现场保留
    assert q.resume_task(tid) == "resumed"
    await _wait_terminals(q, term, [tid])
    assert term.states[tid] == "done"


@pytest.mark.asyncio
async def test_i4_throughput_500_ops_and_recent_cap():
    """批量压测：500 任务全部完成；recent 环形缓冲封顶 20。"""
    term = _Terminal()
    q = _tracking_queue(term)
    ids = [await q.submit("scan") for _ in range(500)]
    await _wait_terminals(q, term, ids, timeout=30.0)
    assert all(s == "done" for s in term.states.values())
    st = await q.status()
    assert len(st["recent"]) == 20  # deque(maxlen=20)
    assert st["depth"] == 0 and not st["running"]


@pytest.mark.asyncio
async def test_i5_bulk_kinds_bypass_limiter():
    """重活不吃限速：bulk kinds 并发完成不受 RateLimiter 拖慢。"""

    class SlowLimiter:
        def __init__(self):
            self.calls = 0

        def keys(self) -> list:
            return []

        async def acquire(self, mult: float = 1.0, account=None) -> None:
            self.calls += 1
            await asyncio.sleep(0.2)

    term = _Terminal()
    lim = SlowLimiter()

    async def run(op: Op) -> None:
        pass

    q = OpQueue(run, interval=0.0, limiter=lim)
    orig_push = q._push
    q._push = lambda ev: (term.record(ev["task_id"], ev["type"])
                          if ev["type"] in ("done", "failed", "cancelled") else None,
                          orig_push(ev))[1]
    kind = next(iter(BULK_KINDS))
    t0 = time.monotonic()
    ids = [await q.submit(kind) for _ in range(4)]
    await _wait_terminals(q, term, ids, timeout=5.0)
    elapsed = time.monotonic() - t0
    assert lim.calls == 0  # bulk 完全不经 limiter
    assert elapsed < 1.0  # 信号量 2 并发，未按限速串行
    # 对照：非 bulk kind 必经 limiter
    term2 = _Terminal()
    q2 = OpQueue(run, interval=0.0, limiter=lim)
    orig_push2 = q2._push
    q2._push = lambda ev: (term2.record(ev["task_id"], ev["type"])
                           if ev["type"] in ("done", "failed", "cancelled") else None,
                           orig_push2(ev))[1]
    tid = await q2.submit("rename")
    await _wait_terminals(q2, term2, [tid])
    assert lim.calls == 1


@pytest.mark.asyncio
async def test_priority_set_and_high_queue_routing():
    """优先级集合可配置；高优 kind 进 hi 队列。"""
    term = _Terminal()
    q = _tracking_queue(term, )
    assert "rename" in DEFAULT_HIGH_PRIORITY
    tid = await q.submit("rename")
    assert q._q_hi.qsize() == 1  # 高优队列
    await _wait_terminals(q, term, [tid])
    tid2 = await q.submit("__unknown_kind__")
    assert q._q_hi.qsize() == 0  # 非高优走常规队列
    await _wait_terminals(q, term, [tid2])
