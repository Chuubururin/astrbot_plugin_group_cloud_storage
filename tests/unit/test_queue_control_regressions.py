"""队列控制回归：resume 回补 _pending（M11）/ _cancelled 无界增长（低危）。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.queue import Op, OpQueue  # noqa: E402


async def _wait(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def _drain(q: OpQueue, n: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await q.status()
        if len(st["recent"]) >= n and not st["running"] and st["depth"] == 0:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"queue drain timeout: {await q.status()}")


@pytest.mark.asyncio
async def test_resume_requeued_op_is_controllable_again():
    """M11 回归：协作式暂停 → 继续后，重新入队的 op 必须重回 _pending。

    修复前 resume 只把 op put 回异步队列、不回补 _pending，于是「继续」与
    worker 再次出队之间的窗口内 pause_task 返回 "unknown"（前端把这次点击
    静默丢弃），cancel_task 也因 _pending 判断不成立而跳过终态写穿（任务页
    一直显示 pending）。
    """
    started = asyncio.Event()
    gate = asyncio.Event()
    entries = {"n": 0}

    async def run(op: Op) -> None:
        entries["n"] += 1
        if entries["n"] == 1:
            started.set()
            while True:  # 检查点抛 OpPausedError -> worker 挂起该 op
                await q.pause_check(op)
                await asyncio.sleep(0.005)
        await gate.wait()  # 恢复后的执行由测试放行

    q = OpQueue(run, interval=0.0, slots=2)
    await q.start()
    tid = await q.submit("move_file")
    await started.wait()
    assert q.pause_task(tid) == "running"
    await _wait(lambda: tid in q._paused)  # handler 已到检查点，op 被挂起
    assert q.resume_task(tid) == "resumed"
    # 此处无 await：worker 来不及再次出队，索引状态就是 resume 留下的状态
    assert tid in q._pending, "resume 未把重新入队的 op 回补进 _pending"
    assert q.pause_task(tid) == "queued"  # 修复前为 "unknown"
    assert q.resume_task(tid) == "resumed"
    gate.set()
    await _drain(q, 1)
    assert entries["n"] >= 2
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_does_not_leak_cancelled_for_dead_ids():
    """低危回归：_cancelled 只为仍在队列/运行中的 op 登记。

    未知 id（已终态/重启残留）与「已出队后暂停挂起」的 op 不在任何队列里，
    登记后永远不会被 _execute 的 finally 清理 -> 集合无界增长。
    """
    gate = asyncio.Event()

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, interval=0.0, slots=2)
    await q.start()
    assert q.cancel_task("no-such-task") is False
    assert "no-such-task" not in q._cancelled

    blocker = await q.submit("move_file")  # 占用唯一的高优 worker
    await _wait(lambda: blocker in q._running)
    tid = await q.submit("move_file")  # 留在高优队列
    assert q.pause_task(tid) == "queued"
    gate.set()
    # worker 出队后挂起该 op：此后它不在任何队列中
    await _wait(lambda: q._paused.get(tid) is not None)
    assert q.cancel_task(tid) is True
    assert tid not in q._cancelled  # 修复前无条件登记 -> 残留
    await _drain(q, 2)
    await q.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_cheap_when_workers_are_idle():
    """正常路径：空闲 worker 一 cancel 就死，shutdown 不得空等宽限期。

    回归守卫：宽限期曾被写成 `await asyncio.sleep(timeout)`，它永不提前退出，
    于是每次 teardown 都白付满额 1.0s —— 单测套件从 17s 涨到 88s（高负载下
    326s），并且打挂了 tests/contract/test_queue_exploratory.py 里
    `assert elapsed < 1.0` 这类与时序无关的断言（实测 1.0206 = 1.0 宽限 +
    20ms 轮询粒度）。
    """
    q = OpQueue(lambda op: asyncio.sleep(0), interval=0.0)
    await q.start()
    await asyncio.sleep(0.05)  # 让 worker 真正停泊在 queue.get() 上
    t0 = time.monotonic()
    await q.shutdown()  # 默认 timeout=1.0
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"shutdown 空等了宽限期: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_a_worker_ignores_cancellation():
    """有界性：连 cancel 都吞掉的 worker 不能把 shutdown 拖成永久挂起。

    回归守卫（版本相关）：asyncio.wait_for 在 CPython <= 3.11 的超时路径是
    _cancel_and_wait -> task.cancel() 然后 await waiter，即"取消之后仍然等它
    结束"。吞掉取消的 worker 永远不会结束，所以 wait_for 根本不是上界 —— CI
    跑 3.10 时整套挂死、本地 3.13 却是绿的（3.12+ 把 wait_for 重建在
    timeouts.timeout 上，丢掉了这个洞）。

    现在的实现用 deadline + asyncio.wait + 重发 cancel，到期明确放弃并告警。
    """
    q = OpQueue(lambda op: asyncio.sleep(0), interval=0.0)
    quit_flag = asyncio.Event()

    async def stubborn() -> None:
        # 永久吞掉取消并重新停泊 —— 这正是"cancel 丢失"之后 worker 的形态。
        # 只有 quit_flag 置位后的那次取消才真正结束它（否则会泄漏到后续用例）；
        # 而"能否被取消掉"恰恰是本用例要证明 shutdown 不依赖的性质。
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                if quit_flag.is_set():
                    raise
                continue

    task = asyncio.create_task(stubborn(), name="op-queue-stubborn")
    q._workers = [task]
    try:
        # 先让桩真正停泊，再证明它确实吞得掉取消 —— 否则本守卫会假绿：
        # create_task 之后立刻 shutdown，_must_cancel 会在协程体执行之前就把
        # 任务打死（CancelledError 抛在 try 之外），桩根本没机会吞，于是旧代码
        # 也能"通过"。
        await asyncio.sleep(0.05)
        assert not task.done(), "stubborn worker 提前结束，守卫无效"
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "stubborn worker 未吞掉取消，守卫无效"

        t0 = time.monotonic()
        await q.shutdown(timeout=0.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"shutdown 被吞取消的 worker 拖住: {elapsed:.3f}s"
    finally:
        quit_flag.set()
        for _ in range(5):
            if task.done():
                break
            task.cancel()
            await asyncio.sleep(0.05)
        assert task.done(), "stubborn worker 未能清理"


@pytest.mark.asyncio
async def test_shutdown_re_cancels_a_worker_that_absorbs_the_first_cancel():
    """重发取消必须真的发生 —— 写在 docstring 里不算数。

    回归守卫（第二轮窗口被第一轮吃光）：第一轮等待曾直接用整个 timeout
    （``asyncio.wait(workers, timeout=timeout)``），一个"吞掉第一次取消"的
    worker 会把这一轮占满到 deadline；第二轮再算 ``remaining = deadline - now``
    必然 <= 0，于是 ``w.cancel()`` 一次都没发出去就 break —— "survivors need
    another round" 成了死代码，worker 被静默放弃（生产里即任务泄漏）。

    上面两条 shutdown 守卫都拦不住它：``test_shutdown_is_cheap_when_workers_
    are_idle`` 的 worker 一 cancel 就死（根本走不到第二轮）；
    ``test_shutdown_is_bounded_when_a_worker_ignores_cancellation`` 只断言
    "耗时 < 2.0s"，而"第一轮空转到 0.5s 后立刻放弃"同样满足它。

    本用例的 worker 只吞掉**第一次**取消，于是：
      * 修复前：第一轮吃满 timeout -> 第二轮不发取消 -> 任务仍在跑（断言 1/2 失败）
      * 修复后：第一轮只等一个切片 -> 第二轮重发取消 -> 任务被真正收走
    """
    q = OpQueue(lambda op: asyncio.sleep(0), interval=0.0)
    quit_flag = asyncio.Event()
    absorbed = {"n": 0}

    async def absorb_once() -> None:
        # "mid-handler 只吸收标志"的最小形态：第一次取消被吞，第二次才生效。
        # quit_flag 兜底，避免本用例自身泄漏到后续用例。
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                absorbed["n"] += 1
                if absorbed["n"] >= 2 or quit_flag.is_set():
                    raise
                continue

    task = asyncio.create_task(absorb_once(), name="op-queue-absorb-once")
    q._workers = [task]
    try:
        await asyncio.sleep(0.05)  # 让桩真正停泊，否则守卫会假绿
        assert not task.done(), "worker 提前结束，守卫无效"

        t0 = time.monotonic()
        await q.shutdown(timeout=1.0)
        elapsed = time.monotonic() - t0

        assert absorbed["n"] >= 2, (
            "重发取消从未执行：worker 只收到 1 次 cancel 就被放弃"
            "（第二轮窗口被第一轮吃光）"
        )
        assert task.done(), "shutdown 返回时 worker 仍存活 —— 任务被静默泄漏"
        assert elapsed < 0.5, f"shutdown 未在切片内收走 worker: {elapsed:.3f}s"
    finally:
        quit_flag.set()
        for _ in range(5):
            if task.done():
                break
            task.cancel()
            await asyncio.sleep(0.05)
        assert task.done(), "absorb-once worker 未能清理"
