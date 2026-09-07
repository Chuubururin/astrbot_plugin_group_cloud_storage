"""任务记录与控制 契约测试（v15，D-6：暂停/继续/中断/撤销 + 操作流）。

用真实 SqliteMetaStore + TaskControlService + OpQueue 端到端验证状态机，
不触碰 OneBot 云端（MyFakeOps 桩替代文件操作执行器）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from core.application.queue import Op, OpQueue  # noqa: E402
from core.application.queue import TaskControlService  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    """store + queue（真实 SQLite）+ task_control（记录挂钩接线）。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    calls: list = []
    queue: OpQueue | None = None
    tc = TaskControlService(store=store, queue=None, ops=None)

    async def run_handler(op: Op):
        # 每步检查点：运行中暂停/中断的协作语义
        while True:
            await queue.pause_check(op)
            n = op.payload.setdefault("_n", 0) + 1
            op.payload["_n"] = n
            if n >= op.payload.get("steps", 0):
                break
            calls.append(op.task_id)
            await asyncio.sleep(0.01)

    class _Led:
        async def on_state(self, task_id, kind, target, payload, state, error=None):
            await tc.on_state(task_id, kind, target, payload, state, error)

        async def on_op(self, task_id, action, before=None, after=None):
            await tc.on_op(task_id, action, before, after)

    queue = OpQueue(
        run_handler=run_handler,
        interval=0.0,
        max_retries=0,
        ledger=_Led(),
    )
    tc.queue = queue
    await queue.start()
    yield SimpleNamespace(store=store, queue=queue, tc=tc, calls=calls)
    await queue.shutdown()
    await store.close()


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _steps(payload: dict, n: int) -> dict:
    payload["steps"] = n
    return payload


async def _wait_state(store, task_id: str, state: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await store.ledger_get(task_id)
        if row is not None and row["state"] == state:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} not in state {state}: {row}")


async def _wait_handler_started(calls: list, min_calls: int = 1, timeout: float = 5.0):
    """等待 run_handler 开始执行（calls 列表为内存信号，避免 SQLite 轮询竞态）。

    运行中协作式语义的观察锚点：handler 每次循环向 calls append task_id；
    出现首个记录即证明任务已进入执行（ledger.running 已写入）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(calls) >= min_calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"handler not started within {timeout}s: calls={len(calls)}")


# ---------- 暂停/继续（排队挂起） ----------

@pytest.mark.asyncio
async def test_pause_queued_then_resume(env):
    # 占满两个高优 worker（槽位 4 = 高优 2），确保被暂停任务必在排队态
    b1 = await env.queue.submit("move_file", "g1", _steps({}, 3))
    b2 = await env.queue.submit("move_file", "g1", _steps({}, 3))
    tp = await env.queue.submit("move_file", "g1", _steps({}, 0))
    assert env.queue.pause_task(tp) == "queued"  # 排队挂起受理
    st = await env.queue.status()
    assert tp in st["paused_ids"]
    await _wait_state(env.store, b1, "done")
    await _wait_state(env.store, b2, "done")
    st = await env.queue.status()
    assert tp in st["paused_ids"]  # 阻塞完成后仍挂起
    assert env.queue.resume_task(tp) == "resumed"
    await _wait_state(env.store, tp, "done")


# ---------- 暂停/继续（运行中协作式） ----------

@pytest.mark.asyncio
async def test_pause_running_cooperative(env):
    # 运行锚点 = handler 内存信号（calls），不做 SQLite running 轮询（规避 WAL 可见性竞态）；
    # pause_task 返回值基于内存 _running 表，可靠；终态（paused/done）经 SQLite 观察稳定。
    t1 = await env.queue.submit("move_file", "g1", _steps({}, 200))
    await _wait_handler_started(env.calls)
    assert env.queue.pause_task(t1) == "running"  # 运行中受理（内存态，非 SQLite）
    await _wait_state(env.store, t1, "paused", timeout=5.0)
    assert env.queue.resume_task(t1) == "resumed"
    await _wait_state(env.store, t1, "done", timeout=8.0)


# ---------- 中断（运行中协作式 + 暂停挂起） ----------

@pytest.mark.asyncio
async def test_interrupt_running(env):
    t1 = await env.queue.submit("move_file", "g1", _steps({}, 200))
    await _wait_handler_started(env.calls)
    assert env.queue.interrupt_task(t1) is True
    await _wait_state(env.store, t1, "cancelled", timeout=5.0)


@pytest.mark.asyncio
async def test_interrupt_paused_hold(env):
    t1 = await env.queue.submit("move_file", "g1", _steps({}, 200))
    env.queue.pause_task(t1)
    await _wait_state(env.store, t1, "paused", timeout=5.0)  # worker 已挂起
    assert env.queue.interrupt_task(t1) is True
    await _wait_state(env.store, t1, "cancelled", timeout=5.0)


# ---------- 记录状态机（经真实 store 全链路） ----------

@pytest.mark.asyncio
async def test_ledger_state_machine_flow(env):
    t1 = await env.queue.submit("move_file", "g1", _steps({}, 1))
    row = await _wait_state(env.store, t1, "done")
    assert row["kind"] == "move_file" and row["target"] == "g1"
    assert row["payload"] == {"steps": 1}
    rows = await env.tc.list_tasks(state="done")
    assert any(r["task_id"] == t1 for r in rows)
    assert (await env.tc.ops("nope")) == []


# ---------- 撤销（D-6 可逆性矩阵） ----------

class MyFakeOps:
    """补偿执行器桩：记录 submit 调用，可供断言。"""

    def __init__(self):
        self.moves: list[tuple] = []
        self.renames: list[tuple] = []

    async def submit_move(self, group_id, id, folder_id):
        self.moves.append((group_id, id, folder_id))
        return "comp_move"

    async def submit_replace_name(self, group_id, id, new_name):
        self.renames.append((group_id, id, new_name))
        return "comp_rename"


@pytest.mark.asyncio
async def test_undo_discard_pending(env):
    # 占满两个高优 worker，确保被撤销任务未开始执行
    await env.queue.submit("move_file", "g1", _steps({}, 3))
    await env.queue.submit("move_file", "g1", _steps({}, 3))
    t1 = await env.queue.submit("move_file", "g1", _steps({}, 0))
    assert env.queue.pause_task(t1) == "queued"  # 挂起未执行
    r = await env.tc.undo(task_id=t1)
    assert r["ok"] is True and r["action"] == "discard"
    await _wait_state(env.store, t1, "cancelled")


@pytest.mark.asyncio
async def test_undo_move_compensation(env):
    fake = MyFakeOps()
    env.tc.file_ops = fake
    # 手工构造已完成移动任务的操作流（真实路径：submit_move 时埋点）
    await env.store.ledger_upsert("t_move", "move_file", "g1", {"id": 7}, "done")
    await env.store.ops_append(
        "t_move", "move",
        before={"group_id": "g1", "id": 7, "folder": ""},
        after={"group_id": "g1", "id": 7, "folder": "dir2"},
    )
    r = await env.tc.undo(task_id="t_move")
    assert r["ok"] is True and r["action"] == "reverse_move"
    assert fake.moves == [("g1", 7, "!/")]  # 根目录 "" → "!/"


@pytest.mark.asyncio
async def test_undo_rename_compensation(env):
    fake = MyFakeOps()
    env.tc.file_ops = fake
    await env.store.ledger_upsert("t_ren", "replace_name", "g1", {"id": 8}, "done")
    await env.store.ops_append(
        "t_ren", "replace_name",
        before={"group_id": "g1", "id": 8, "name": "old.txt"},
        after={"group_id": "g1", "id": 8, "name": "new.txt"},
    )
    r = await env.tc.undo(task_id="t_ren")
    assert r["ok"] is True and r["action"] == "restore_name"
    assert fake.renames == [("g1", 8, "old.txt")]


@pytest.mark.asyncio
async def test_undo_delete_not_undoable(env):
    await env.store.ledger_upsert("t_del", "delete", "g1", {"id": 9}, "done")
    await env.store.ops_append(
        "t_del", "delete",
        before={"group_id": "g1", "id": 9, "name": "x.zip"}, after={},
    )
    r = await env.tc.undo(task_id="t_del")
    assert r["ok"] is False and r["undoable"] is False
    assert "不可逆" in r["reason"]


@pytest.mark.asyncio
async def test_undo_unknown_kind_not_undoable(env):
    await env.store.ledger_upsert("t_up", "upload", "g1", {"id": 10}, "done")
    r = await env.tc.undo(task_id="t_up")
    assert r["ok"] is False and r["undoable"] is False


@pytest.mark.asyncio
async def test_undo_tags_snapshot_restore(env):
    await env.store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                 source_ref="r1", size=1, uploader_id="u1",
                 uploader_name="Alice", busid=0, created_at=1),
    ])
    rid = (await env.store.query_resources(
        ResourceQuery(group_id="g1", page_size=10)
    )).items[0].id
    await env.store.ops_append(
        "", "tags",
        before={"group_id": "g1", "id": rid, "tags": ["old"]},
        after={"group_id": "g1", "id": rid, "tags": ["old", "new"]},
    )
    # 快照恢复前给资源写上新标签（模拟页面已改）
    await env.store.update_resource_tags(rid, ["old", "new"])
    r = await env.tc.undo(group_id="g1", resource_id=rid)
    assert r["ok"] is True and r["action"] == "tags_restored"
    detail = await env.store.get_resource_detail("g1", rid)
    assert json.loads(detail["tags"]) == ["old"]
    # 可翻转：再次撤销回到新标签
    r2 = await env.tc.undo(group_id="g1", resource_id=rid)
    assert r2["ok"] is True and r2["action"] == "tags_restored"
    detail = await env.store.get_resource_detail("g1", rid)
    assert json.loads(detail["tags"]) == sorted(["old", "new"])