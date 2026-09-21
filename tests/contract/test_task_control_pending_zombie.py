"""重启对账遗留 pending 行的撤销收敛（task_control 回归）。

真机场景：重启后 ledger_reconcile 把 convert_volumes/video_upload/netdisk_index
这类断点任务从 running 置回 pending（outbox.ledger_reconcile），但没有任何人
重新提交它们；撤销对这类行只回 {"ok": false, "action": "discard"}，行却仍是
pending -> 永久卡住。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.queue import OpQueue, TaskControlService  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    hold = asyncio.Event()  # 阻塞 handler，把 worker 占住以便构造排队态

    async def run_handler(op):
        await hold.wait()

    queue = OpQueue(run_handler=run_handler, interval=0.0, max_retries=0)
    tc = TaskControlService(store=store, queue=queue, ops=None)
    yield store, queue, tc
    await queue.shutdown()
    await store.close()


async def _wait_running(queue, n: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(queue._running) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("workers did not pick up the blockers")


@pytest.mark.asyncio
async def test_undo_restart_reconciled_pending_row_converges(env):
    store, _, tc = env
    await store.ledger_upsert("t_bp", "convert_volumes", "g1", {"id": 1}, "running")
    await store.ledger_reconcile()
    row = await store.ledger_get("t_bp")
    assert row["state"] == "pending"  # 重启后的真实状态（断点类）

    r = await tc.undo(task_id="t_bp")
    assert r["ok"] is True and r["action"] == "discard"
    row = await store.ledger_get("t_bp")
    # 修复前仍是 pending（无人重新提交 -> 永久卡住）
    assert row["state"] == "failed"
    assert row["payload"] == {"id": 1}


@pytest.mark.asyncio
async def test_undo_live_queued_row_still_discards(env):
    """回归保护：真正在队列中的 pending 行仍走 interrupt（不误标 failed）。"""
    store, queue, tc = env
    # 占满两个高优 worker，使 tid 必在排队态（不被执行）
    await queue.submit("move_file", "g1")
    await queue.submit("move_file", "g1")
    await _wait_running(queue, 2)
    tid = await queue.submit("move_file", "g1")
    # 本 fixture 未接台账挂钩，手工写入排队中的 pending 行
    await store.ledger_upsert(tid, "move_file", "g1", {}, "pending")
    assert queue.pause_task(tid) == "queued"  # 挂起，保证它不会被执行
    r = await tc.undo(task_id=tid)
    assert r["ok"] is True and r["action"] == "discard"
    assert "未执行" in r["note"]
    # 返回字面不能证明 discard 真的生效：旧版只查 response，撤销没落地也绿。
    # 反向验证：让 undo 不调 interrupt_task 直接回 discard，下面三条变红。
    assert tid not in queue._pending, "撤销后不得仍留在排队集合（worker 仍会拉起它）"
    assert tid not in queue._paused, "撤销后不得仍挂在暂停集合"
    assert tid not in queue._running
