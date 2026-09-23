"""R-3 回归：任务索引的清理对称性（M11 家族）。

背景
----
OpQueue 用 5 个内存索引追踪任务：

    _pending   set      已提交、尚未开始执行（排队中 / 重试退避 / 暂停重排）
    _cancelled set      取消标记（出队时消费）
    _paused    dict     暂停挂起；None = 仍在异步队列里的占位符
    _running   dict     正在执行的 op
    _ops_by_id dict     权威索引：has_pending() / pause_task() / cancel_task() 都读它

**单个索引漏清 = 僵尸条目**，后果按严重度递增：
- `_ops_by_id` 漏清 → `has_pending()` 永远为真 → **所有自动提交被永久阻塞**
  （这是最严重的一类，`_pending` 漏清只影响 pause 判定的准确性）。
- `_paused` 漏清 → `status()["paused_ids"]` 常驻；`resume_task` 返回 "resumed" 却无对象。
- `_running` 漏清 → `status()["running"]` 常驻，任务页显示永不结束的任务。
- `_cancelled` 漏清 → 无界增长（已有 test_cancel_does_not_leak_cancelled_for_dead_ids 覆盖）。

本文件的判据是**不变量**而不是逐点断言：走完任一条退出路径并等队列静默后，
该 task_id 必须**不在任何一个索引里**。这样一次网住所有不对称，而不是
每发现一处补一条测试。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.queue import Op, OpQueue  # noqa: E402

_INDEX_NAMES = ("_pending", "_cancelled", "_paused", "_running", "_ops_by_id")

# 两个 worker 池的清理逻辑是**重复代码**（_worker_loop_hi / _worker_loop），
# 一旦只改一处就会出现池间不对称。下面用两个池各自的 kind 参数化，确保两条
# 路径都被覆盖。注意 `move_file` 在 DEFAULT_HIGH_PRIORITY 内（走 _q_hi），
# 而 `sync` 不在（走 _q）；只用前者会漏掉整个低优先级池。
HI_KIND = "move_file"   # DEFAULT_HIGH_PRIORITY 内 -> _worker_loop_hi
LO_KIND = "sync"        # DEFAULT_HIGH_PRIORITY 外 -> _worker_loop


def _leftovers(q: OpQueue, tid: str) -> list[str]:
    """tid 仍残留在哪些索引里（空列表 = 干净）。"""
    out = []
    for name in _INDEX_NAMES:
        idx = getattr(q, name)
        if tid in idx:
            out.append(name)
    return out


async def _quiet(q: OpQueue, timeout: float = 8.0) -> None:
    """等到队列完全静默：深度 0、无 running、无 pending。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await q.status()
        if st["depth"] == 0 and not st["running"] and not q._pending:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"queue not quiet: {await q.status()}")


async def _wait(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- 终态路径


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_success_path_leaves_no_index_entry(kind):
    """正常成功：终态后五个索引都不应留下 tid。"""
    run_calls = {"n": 0}

    async def run(op: Op) -> None:
        run_calls["n"] += 1

    q = OpQueue(run, slots=2)
    await q.start()
    tid = await q.submit(kind)
    await _quiet(q)
    assert run_calls["n"] == 1
    assert _leftovers(q, tid) == [], f"成功路径残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_failed_path_leaves_no_index_entry(kind):
    """终态失败（不可重试）：终态后五个索引都不应留下 tid。"""

    async def run(op: Op) -> None:
        raise ValueError("deterministic failure")

    q = OpQueue(run, slots=2, max_retries=0)
    await q.start()
    tid = await q.submit(kind)
    await _quiet(q)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"
    assert _leftovers(q, tid) == [], f"失败路径残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_retry_exhausted_leaves_no_index_entry(kind):
    """重试耗尽：每次重试都要保持 _ops_by_id/_pending 在册，终态后必须全清。"""

    async def run(op: Op) -> None:
        raise ValueError("always fails")

    q = OpQueue(run, slots=2, max_retries=1, backoff_base=0.05)
    await q.start()
    tid = await q.submit(kind)
    # 退避窗口内必须仍可被控制（M11：retry 期间不得脱离索引）
    await _wait(lambda: tid in q._ops_by_id)
    await _quiet(q)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"
    assert _leftovers(q, tid) == [], f"重试耗尽后残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_cancel_while_queued_leaves_no_index_entry(kind):
    """排队中取消：深度队列里直接写终态，且不得残留 _cancelled。"""
    gate = asyncio.Event()

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, slots=1)
    await q.start()
    first = await q.submit(kind)  # 占住唯一的 worker
    await _wait(lambda: first in q._running)
    second = await q.submit(kind)  # 纯排队
    await _wait(lambda: second in q._pending)
    assert q.cancel_task(second) is True
    # 中间态：终态已写穿（_ops_by_id 立即弹出），但 op 还在异步队列里，
    # `_cancelled` 必须保留 —— worker 出队时靠它走取消分支。清理在出队后。
    assert second not in q._ops_by_id, "排队取消必须立即写穿终态并弹出权威索引"
    assert second in q._cancelled, "_cancelled 必须保留到出队消费（否则会执行已取消的任务）"
    gate.set()
    await _quiet(q)
    assert _leftovers(q, first) == []
    assert _leftovers(q, second) == [], f"排队取消残留索引: {_leftovers(q, second)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_cancel_running_leaves_no_index_entry(kind):
    """运行中取消（协作式）：检查点抛 OpCancelError，终态后全清。"""
    started = asyncio.Event()

    async def run(op: Op) -> None:
        started.set()
        while True:
            await q.pause_check(op)  # op.cancel -> OpCancelError
            await asyncio.sleep(0.005)

    q = OpQueue(run, slots=1)
    await q.start()
    tid = await q.submit(kind)
    await started.wait()
    assert q.cancel_task(tid) is True
    await _quiet(q)
    st = await q.status()
    assert st["recent"][0]["state"] == "cancelled"
    assert _leftovers(q, tid) == [], f"运行中取消残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_pause_then_cancel_hold_leaves_no_index_entry(kind):
    """暂停挂起后取消：held op 不在任何队列里，必须直接清干净。"""
    started = asyncio.Event()

    async def run(op: Op) -> None:
        started.set()
        while True:
            await q.pause_check(op)
            await asyncio.sleep(0.005)

    q = OpQueue(run, slots=1)
    await q.start()
    tid = await q.submit(kind)
    await started.wait()
    assert q.pause_task(tid) == "running"
    await _wait(lambda: tid in q._paused and q._paused[tid] is not None)
    assert q.cancel_task(tid) is True
    await _quiet(q)
    st = await q.status()
    assert st["recent"][0]["state"] == "cancelled"
    assert _leftovers(q, tid) == [], f"暂停挂起后取消残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_cancel_queued_pause_placeholder_leaves_no_index_entry(kind):
    """排队暂停（None 占位符）后取消：占位符必须被弹掉，索引全清。"""
    gate = asyncio.Event()

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, slots=1)
    await q.start()
    first = await q.submit(kind)
    await _wait(lambda: first in q._running)
    second = await q.submit(kind)
    await _wait(lambda: second in q._pending)
    assert q.pause_task(second) == "queued"
    assert q._paused.get(second, "missing") is None, "应为 None 占位符"
    assert q.cancel_task(second) is True
    assert _leftovers(q, second) == [], f"占位符取消残留索引: {_leftovers(q, second)}"
    gate.set()
    await _quiet(q)
    assert _leftovers(q, second) == []
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_pause_resume_then_success_leaves_no_index_entry(kind):
    """暂停 → 恢复 → 成功的完整回路：终态后全清，且 _paused 不得留条目。"""
    started = asyncio.Event()
    gate = asyncio.Event()
    entries = {"n": 0}

    async def run(op: Op) -> None:
        entries["n"] += 1
        if entries["n"] == 1:
            started.set()
            while True:
                await q.pause_check(op)
                await asyncio.sleep(0.005)
        await gate.wait()

    q = OpQueue(run, slots=1)
    await q.start()
    tid = await q.submit(kind)
    await started.wait()
    assert q.pause_task(tid) == "running"
    await _wait(lambda: tid in q._paused)
    assert q.resume_task(tid) == "resumed"
    gate.set()
    await _quiet(q)
    assert entries["n"] >= 2
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    assert _leftovers(q, tid) == [], f"暂停恢复后残留索引: {_leftovers(q, tid)}"
    await q.shutdown()


# ------------------------------------------------- 跨任务：无累积泄漏


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_many_tasks_leave_indices_empty(kind):
    """批量终态后，所有索引必须回到空集（防逐任务小泄漏累积）。"""

    async def run(op: Op) -> None:
        await asyncio.sleep(0)

    q = OpQueue(run, slots=4)
    await q.start()
    tids = [await q.submit(kind) for _ in range(25)]
    await _quiet(q)
    for tid in tids:
        assert _leftovers(q, tid) == [], f"{tid} 残留: {_leftovers(q, tid)}"
    assert q._pending == set(), f"_pending 未回到空集: {q._pending}"
    assert q._running == {}, f"_running 未回到空集: {q._running}"
    assert q._paused == {}, f"_paused 未回到空集: {q._paused}"
    assert q._cancelled == set(), f"_cancelled 未回到空集: {q._cancelled}"
    assert q._ops_by_id == {}, f"_ops_by_id 未回到空集: {q._ops_by_id}"
    await q.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HI_KIND, LO_KIND])
async def test_invariant_terminal_task_does_not_block_has_pending(kind):
    """最严重后果的守门测试：终态任务不得让 has_pending() 永真。

    `has_pending` 遍历 `_ops_by_id`。该索引泄漏 = 自动提交被永久阻塞
    （用户侧表现为「加了新任务但队列再也不接新活了」），且没有任何报错。
    """

    async def run(op: Op) -> None:
        await asyncio.sleep(0)

    q = OpQueue(run, slots=2)
    await q.start()
    tid = await q.submit(kind, target="g1", payload={"id": 7})
    assert q.has_pending(kind, "id", 7) is True
    await _quiet(q)
    assert q.has_pending(kind, "id", 7) is False, (
        "终态任务仍被 has_pending 命中：_ops_by_id 泄漏会永久阻塞自动提交"
    )
    assert _leftovers(q, tid) == []
    await q.shutdown()
