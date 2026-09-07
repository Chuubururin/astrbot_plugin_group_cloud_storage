"""混沌测试（M0）：风控重试 / 环境态不重试 / 遍历中途失败一致性 / 限速等待期取消。

评审确认的薄弱点：网络抖动、风控、部分失败、取消场景的回归保护。
故障注入通过测试内子类化 FakeOneBotApi 实现（不改动共享夹具）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import (OneBotApiError, OneBotErrorKind,  # noqa: E402
                               ResourceStatus, ResourceType, SyncStatus)
from core.domain.resource import Resource  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi, build_tree  # noqa: E402


async def _collect_until(queue: OpQueue, stop_types: set[str],
                         timeout: float = 15.0) -> list[dict]:
    """订阅事件流直到命中 stop_types；返回已收集事件。"""
    events: list[dict] = []

    async def listener():
        async for ev in queue.subscribe():
            events.append(ev)
            if ev.get("type") in stop_types:
                return

    task = asyncio.create_task(listener())
    try:
        await asyncio.wait_for(task, timeout)
    finally:
        if not task.done():
            task.cancel()
    return events


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


class _FlakyHandler:
    """前 N 次调用抛 RATE_LIMITED（模拟上层透传风控错误），之后正常同步。"""

    def __init__(self, svc: ResourceSyncService, fail_first: int = 2):
        self._svc = svc
        self._fail_first = fail_first
        self.results = []

    async def __call__(self, op):
        if self._fail_first > 0:
            self._fail_first -= 1
            raise OneBotApiError(
                OneBotErrorKind.RATE_LIMITED, "get_group_root_files", "simulated"
            )
        self.results.append(
            await self._svc.run_full_sync(op.target, asyncio.Lock())
        )


@pytest.mark.asyncio
async def test_rate_limited_retries_then_succeeds(store):
    """风控抖动：前 2 次 RATE_LIMITED 指数退避重试，第 3 次成功 → done。"""
    api = FakeOneBotApi(build_tree(file_total=40, folder_total=2, files_per_folder=10))
    handler = _FlakyHandler(ResourceSyncService(api, store), fail_first=2)

    queue = OpQueue(handler, interval=0.0, max_retries=3, backoff_base=0.05)
    try:
        listener = asyncio.create_task(_collect_until(queue, {"done", "failed"}))
        await asyncio.sleep(0.02)
        await queue.submit("sync", target="g1")
        events = await listener
        retries = [e for e in events if e.get("type") == "retry"]
        assert len(retries) == 2
        assert events[-1]["type"] == "done"
        assert handler.results and handler.results[0].status == SyncStatus.OK
        assert handler.results[0].files_found == 20
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_local_error_not_retried(store):
    """环境态错误（缺 bot 上下文）：立即 failed，零重试。"""
    calls = []

    async def run(op):
        calls.append(1)
        raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, "scan", "no bot")

    queue = OpQueue(run, interval=0.0, max_retries=5, backoff_base=0.02)
    try:
        listener = asyncio.create_task(_collect_until(queue, {"done", "failed"}))
        await asyncio.sleep(0.02)
        await queue.submit("scan", target="g1")
        events = await listener
        assert not any(e.get("type") == "retry" for e in events)
        assert events[-1]["type"] == "failed"
        assert len(calls) == 1
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_traversal_failure_no_orphan_cleanup(store):
    """遍历中途失败：SyncResult FAILED 且孤儿清理未执行（既有 active 行保留）。"""
    old = Resource(
        group_id="g1", type=ResourceType.FILE, name="old.txt",
        source_ref="old_file", size=1, uploader_id="10001", created_at=1,
    )
    await store.upsert_resources([old])
    api = FakeOneBotApi(build_tree(file_total=40, folder_total=2, files_per_folder=10),
                        fail_folders={"folder_1"})
    svc = ResourceSyncService(api, store)
    results = []

    async def run(op):
        results.append(await svc.run_full_sync(op.target, asyncio.Lock()))

    queue = OpQueue(run, interval=0.0, max_retries=0)
    try:
        await queue.submit("sync", target="g1")
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            st = await queue.status()
            if len(st["recent"]) >= 1 and not st["running"]:
                break
            await asyncio.sleep(0.05)
        assert results and results[0].complete is False
        detail = await store.get_resource_detail("g1", 1)
        assert detail["status"] == ResourceStatus.ACTIVE.value  # 未误删
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_limiter_wait(store):
    """限速等待期取消：cancelled 事件出现且 handler 从未执行。"""
    ran = []

    async def run(op):
        ran.append(op.kind)

    queue = OpQueue(run, interval=0.4)
    try:
        listener = asyncio.create_task(_collect_until(queue, {"cancelled", "done"}))
        await asyncio.sleep(0.02)
        tid = await queue.submit("sync", target="g1")
        assert queue.cancel_task(tid) is True
        events = await asyncio.wait_for(listener, timeout=10.0)
        assert events[-1]["type"] == "cancelled"
        assert ran == []
    finally:
        await queue.shutdown()
