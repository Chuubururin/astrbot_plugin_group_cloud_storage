"""WebAPI handler 契约：files/batch-tags 的撤销快照（undo 闭环）。

批量打标签走本地直写，撤销依赖 op_ops 的 before/after 快照（与单文件
files/tags 端点同构）。回放 2026-09-11 真机坏链：batch-tags 漏写快照时
tasks/undo {group_id, id} 报「无标签操作流记录」，撤销链断裂。

Run: pytest tests/contract/test_webapi_batch_tags.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from webapi.resources_mutation import api_files_batch_tags  # noqa: E402


def _json_response(data):
    return data


def _error_response(msg, status_code=400):
    return {"status": "error", "message": msg, "status_code": status_code}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_resources([
        Resource(
            group_id="g1", type=ResourceType.FILE, name="a.txt",
            source_ref="r1", size=1, busid=0, created_at=1,
            tags=["旧标签"], status=ResourceStatus.ACTIVE,
        ),
        Resource(
            group_id="g1", type=ResourceType.FILE, name="b.txt",
            source_ref="r2", size=2, busid=0, created_at=1,
            status=ResourceStatus.ACTIVE,
        ),
    ])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10)
    )
    rid_a = next(i.id for i in page.items if i.name == "a.txt")
    rid_b = next(i.id for i in page.items if i.name == "b.txt")

    monkeypatch.setattr("webapi.resources_mutation.json_response", _json_response)
    monkeypatch.setattr("webapi.resources_mutation.error_response", _error_response)

    body: dict = {}

    async def _json_body():
        return body

    fake = SimpleNamespace(query=SimpleNamespace(get=lambda k, d=None, type=None: None))
    async def _no_json(default=None):
        return body
    fake.json = _no_json
    # core.api_validate.json_body reads the astrbot request binding; patch it
    # to return the test body directly.
    import core.api_validate as _av
    monkeypatch.setattr(_av, "request", fake)
    monkeypatch.setattr("webapi.resources_mutation.json_body", _json_body)
    # _managed_items reads the group gate via webapi_base._group_open_error;
    # patch the name bound into resources_mutation.
    async def _gate_ok(s, group):
        return None
    monkeypatch.setattr("webapi.resources_mutation._group_open_error", _gate_ok)
    monkeypatch.setattr("webapi.webapi_base._param", _fake_param(body))

    recorded: list[dict] = []

    class _Queue:
        async def record_op(self, task_id, action, before=None, after=None):
            recorded.append({"task_id": task_id, "action": action,
                             "before": before, "after": after})
            # 生产 OpQueue.record_op 透传到台账 on_op -> ops_append；
            # stub 同步落库，让 undo 的 ops_last_for_resource 通道真实可用。
            await store.ops_append(task_id, action, before, after)

    async def _open_ok(group_id, managed_groups):
        return None  # all groups openable in tests

    s = SimpleNamespace(store=store, queue=_Queue(),
                        scan=SimpleNamespace(assert_group_openable=_open_ok))
    yield SimpleNamespace(store=store, s=s, body=body, recorded=recorded,
                          rid_a=rid_a, rid_b=rid_b)
    await store.close()


def _fake_param(body: dict):
    async def _param(key: str, default: str = "") -> str:
        v = body.get(key)
        return str(v) if v is not None else default
    return _param


@pytest.mark.asyncio
async def test_batch_tags_records_undo_snapshot(env):
    """批量打标签必须逐项写 before/after 快照（撤销链前提）。"""
    env.body.update({
        "items": [{"id": env.rid_a, "group": "g1"},
                  {"id": env.rid_b, "group": "g1"}],
        "tags": ["新标签"],
    })
    out = await api_files_batch_tags(env.s)
    assert out["updated"] == 2 and out["skipped"] == [], f"out={out}"

    # 写入生效
    d = await env.store.get_resource_detail("g1", env.rid_a)
    assert json.loads(d["tags"]) == ["新标签"]

    # 快照逐项落库：before 带各自旧标签（a 有旧标签，b 为空）
    assert len(env.recorded) == 2
    snap_a = next(r for r in env.recorded if r["before"]["id"] == env.rid_a)
    assert snap_a["action"] == "tags" and snap_a["task_id"] == ""
    assert snap_a["before"]["tags"] == ["旧标签"]
    assert snap_a["after"]["tags"] == ["新标签"]
    snap_b = next(r for r in env.recorded if r["before"]["id"] == env.rid_b)
    assert snap_b["before"]["tags"] == []

    # 撤销通道可用：ops_last_for_resource 能按资源定位快照
    op = await env.store.ops_last_for_resource("tags", env.rid_a)
    assert op is not None and op["after"]["tags"] == ["新标签"]


@pytest.mark.asyncio
async def test_batch_tags_undo_restores_previous_tags(env):
    """端到端：batch-tags → undo 快照恢复 → 可反复翻转。"""
    from core.application.queue.task_control import TaskControlService

    env.body.update({
        "items": [{"id": env.rid_a, "group": "g1"}],
        "tags": ["新标签"],
    })
    await api_files_batch_tags(env.s)

    tc = TaskControlService(env.store, queue=None)
    r = await tc.undo(group_id="g1", resource_id=env.rid_a)
    assert r["ok"] is True and r["action"] == "tags_restored"
    d = await env.store.get_resource_detail("g1", env.rid_a)
    assert json.loads(d["tags"]) == ["旧标签"]
