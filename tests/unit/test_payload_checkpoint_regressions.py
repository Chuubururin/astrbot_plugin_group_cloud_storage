"""P0 落盘回归：运行期 payload 增量必须在状态迁移前落库。

背景（ledger 写入时机）
--------------------
``core/application/queue/execution.py`` 只在**状态迁移**处写 ledger：

    running(182) -> _run_handler(183) -> done(213) | paused(237)
                                       | retry(283) | failed(317)

每次写入都是把 ``op.payload`` 的**当前快照**整个 JSON 序列化进 ``op_ledger``。
handler 运行中往 ``op.payload`` 塞的"已做过"标记若只停在内存，则：

* **pause** 路径侥幸能存活 —— ``pause_check`` 抛 ``OpPausedError`` 会触发 paused 写入，
  那时 payload 快照已包含运行期增量；
* **crash / kill** 路径不行 —— 没有任何后续状态迁移，重启后读到的还是
  ``_ledger_state(op, "running")`` 在 handler **开跑前**留下的那份旧快照。

两个已在生产路径上的缺陷都源于此：

1. ``essence_save`` 的 ``sent_parts``（已发段回执）丢失 -> 崩溃重入从第 1 段重发，
   群里出现重复精华消息；第一批 ``message_id`` 只存在于内存，插件再也删不掉那批孤儿。
2. ``upload`` 的 ``parent_resource_id`` 丢失 -> 崩溃重入时 ``crud.py`` 判定"还没建父资源"，
   再建一个 ``volgroup:`` 父资源，第一个连同它已上传的分片一起被孤立。

修法：``OpQueue.checkpoint_payload(op)`` —— 复用 ``running`` 态写库（upsert 幂等），
不引入新状态、不改 schema；handler 在**非幂等副作用之后**立刻调用一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.queue import OpQueue  # noqa: E402


def test_essence_persists_sent_parts_before_next_transition():
    """essence_save 每发完一段就必须落盘。"""
    src = Path("core/application/ingest/essence.py").read_text(encoding="utf-8")
    assert "checkpoint_payload" in src, (
        "essence_save 未在写入 sent_parts 后落盘：崩溃重入会重发全部段落，"
        "第一批 message_id 只存在于内存，变成无法删除的孤儿消息"
    )
    # 必须在 sent_parts 赋值之后紧邻调用，而不是散落在别处
    idx_assign = src.index('op.payload["sent_parts"] = [')
    idx_cp = src.index("checkpoint_payload", idx_assign)
    between = src[idx_assign:idx_cp]
    assert "checkpoint_payload" not in src[:idx_assign], (
        "checkpoint_payload 出现在 sent_parts 赋值之前，顺序不对"
    )
    assert between.count("\n") < 16, (
        f"checkpoint_payload 距 sent_parts 赋值过远（{between.count(chr(10))} 行），"
        "崩溃窗口没被塌缩"
    )
    # 赋值块本身要闭合，确认索引落在正确的位置
    assert "for s in sorted(sent)" in between, "sent_parts 赋值块结构异常"


def test_upload_persists_parent_resource_id_before_volume_upload():
    """upload 建完卷父资源后必须立刻落盘，否则崩溃重入会再建一个孤儿父资源。"""
    src = Path("core/application/files/crud.py").read_text(encoding="utf-8")
    assert "checkpoint_payload" in src, (
        "upload 未在写入 parent_resource_id 后落盘："
        "崩溃重入会重复创建 volgroup 父资源并孤立第一个"
    )
    idx_assign = src.index('op.payload["parent_resource_id_full"]')
    idx_cp = src.index("checkpoint_payload", idx_assign)
    between = src[idx_assign:idx_cp]
    assert between.count("\n") < 12, (
        f"checkpoint_payload 距 parent_resource_id_full 赋值过远"
        f"（{between.count(chr(10))} 行）"
    )
    # 父资源入库必须先于落盘：否则落盘了却没建行，重入会用不存在的 parent
    idx_insert = src.index("await self.store.upsert_resources(", idx_assign - 2000)
    assert idx_insert < idx_cp, "应先建父资源行再落盘 parent_resource_id"


def test_checkpoint_payload_reuses_running_state():
    """checkpoint_payload 复用 running 态写库：不引入新状态、不改 schema。"""
    import inspect

    assert hasattr(OpQueue, "checkpoint_payload"), "OpQueue 缺少 checkpoint_payload"
    sig = inspect.signature(OpQueue.checkpoint_payload)
    assert list(sig.parameters) == ["self", "op"], f"签名异常: {list(sig.parameters)}"
    body = inspect.getsource(OpQueue.checkpoint_payload)
    assert '"running"' in body, "应复用 running 态写入（upsert 幂等）"
    # 只应委托给 _ledger_state，不应自己拼 SQL 或新增状态
    assert body.count("_ledger_state") == 1, "checkpoint_payload 应只委托一次 _ledger_state"


@pytest.mark.asyncio
async def test_checkpoint_payload_actually_writes_ledger():
    """行为验证：调用后 ledger 里立刻能看到运行期新加的 payload 键。"""
    from core.application.queue.op import Op

    class _Ledger:
        def __init__(self):
            self.calls: list[tuple] = []

        async def on_state(self, task_id, kind, target, payload, state, error=None):
            self.calls.append((task_id, dict(payload), state))

        async def on_op(self, *a, **k):  # pragma: no cover - unused here
            pass

    ledger = _Ledger()

    async def _noop(op):  # pragma: no cover - never run
        return None

    q = OpQueue(_noop, ledger=ledger)
    o = Op(task_id="t1", kind="essence_save", target="g1", payload={"title": "T"})

    await q.checkpoint_payload(o)
    assert ledger.calls, "未触发 ledger 写入"
    assert ledger.calls[-1][2] == "running"

    # 模拟 handler 运行期新增 payload 键（发出第 1 段）
    o.payload["sent_parts"] = [{"seq": 1, "message_id": "m1", "chars": 3}]
    await q.checkpoint_payload(o)
    _tid, snap, state = ledger.calls[-1]
    assert snap.get("sent_parts") == o.payload["sent_parts"], (
        "checkpoint_payload 未把运行期新增的 payload 键写进 ledger"
    )
    assert state == "running"


@pytest.mark.asyncio
async def test_ledger_survives_crash_and_marks_part_as_sent(tmp_path):
    """端到端：走真 SQLite ledger，崩溃后重入能从 payload 读回 sent_parts。"""
    from adapters.persistence.sqlite import SqliteMetaStore
    from core.application.queue.op import Op

    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()

    class _RealLedger:
        """把 TaskControlService 的 on_state 语义接到真 store 上。"""

        async def on_state(self, task_id, kind, target, payload, state, error=None):
            await store.ledger_upsert(task_id, kind, target, payload, state, error=error)

        async def on_op(self, *a, **k):  # pragma: no cover
            pass

    async def _noop(op):  # pragma: no cover
        return None

    q = OpQueue(_noop, ledger=_RealLedger())
    o = Op(task_id="t-crash", kind="essence_save", target="g1",
           payload={"title": "T", "text": "x" * 50})

    # execution.py:182 —— handler 开跑前的 running 快照
    await q.checkpoint_payload(o)
    # handler 发出第 1 段 -> 写 payload -> 立即落盘（本次修复）
    o.payload["sent_parts"] = [{"seq": 1, "message_id": "mid-1", "chars": 50}]
    await q.checkpoint_payload(o)
    # 此刻进程崩溃（无 done/failed 写入）

    row = await store.ledger_get("t-crash")
    assert "sent_parts" in row["payload"], (
        "崩溃后 ledger 里没有 sent_parts -> 重入会重发第 1 段"
    )
    assert row["payload"]["sent_parts"][0]["message_id"] == "mid-1"

    # 重入：从 ledger 恢复 payload，应能识别第 1 段已发
    restored = Op(task_id="t-crash", kind=o.kind, target=o.target,
                  payload=row["payload"])
    sent = {
        int(p["seq"]): str(p.get("message_id") or "")
        for p in (restored.payload.get("sent_parts") or [])
        if p.get("seq")
    }
    assert 1 in sent, "重入未能识别第 1 段已发送 -> 会重复发送"
    assert sent[1] == "mid-1", "重入拿不到原 message_id -> 孤儿消息无法删除"

    await store.close()
