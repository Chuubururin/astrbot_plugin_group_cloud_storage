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
