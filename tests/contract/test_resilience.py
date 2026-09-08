"""S6 韧性测试：错误分类 / 退避 / 限速 / 事件容错 / 任务取消（docs/05 §2 S6）。

覆盖失败分类（DoD #8）：
- UNSUPPORTED / TIMEOUT / REMOTE_ERROR / RATE_LIMITED 统一为 OneBotApiError
- BROKEN 退避（backoff 期间抛 REMOTE_ERROR，不重试）
- 限速间隔（IntervalLimiter）
- 事件索引容错（非 group_upload 返回 False）
- 同步任务取消 → sync_logs 终态 cancelled
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.limiter.interval import IntervalLimiter  # noqa: E402
from adapters.onebot.napcat import NapCatApiAdapter  # noqa: E402
from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import CapabilityState, OneBotApiError, OneBotErrorKind, SyncStatus  # noqa: E402
from core.domain.resource import GroupFileList  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


# ---------- NapCatApiAdapter 错误分类 ----------

async def _call_impl(err: Exception | None = None, delay: float = 0, calls: list = None):
    """构造 call_action 实现：可注入异常/延迟/记录调用。"""

    async def impl(action: str, **params):
        if calls is not None:
            calls.append((action, time.monotonic()))
        if delay:
            await asyncio.sleep(delay)
        if err is not None:
            raise err
        if action == "get_group_root_files":
            return {"files": [], "folders": []}
        if action == "get_group_file_system_info":
            return {"file_count": 0, "limit_count": 100, "used_space": 0, "total_space": 100}
        return {}

    return impl


@pytest.mark.asyncio
async def test_unsupported_classification():
    impl = await _call_impl(err=RuntimeError("API not found: get_group_root_files"))
    api = NapCatApiAdapter(impl, interval=0)
    with pytest.raises(OneBotApiError) as ei:
        await api.list_group_root("g1")
    assert ei.value.kind == OneBotErrorKind.UNSUPPORTED
    assert api.capability("get_group_root_files") == CapabilityState.UNSUPPORTED


@pytest.mark.asyncio
async def test_timeout_classification_and_backoff():
    impl = await _call_impl(err=RuntimeError("request timeout after 3s"))
    api = NapCatApiAdapter(impl, interval=0)
    with pytest.raises(OneBotApiError) as ei:
        await api.list_group_root("g1")
    assert ei.value.kind == OneBotErrorKind.TIMEOUT
    assert api.capability("get_group_root_files") == CapabilityState.UNKNOWN  # 新语义：不标记能力
    # 新语义：无适配器级退避；第二次调用仍为 TIMEOUT（由 OpQueue 层有限重试）
    with pytest.raises(OneBotApiError) as ei2:
        await api.list_group_root("g1")
    assert ei2.value.kind == OneBotErrorKind.TIMEOUT


@pytest.mark.asyncio
async def test_remote_error_classification():
    impl = await _call_impl(err=RuntimeError("boom"))
    api = NapCatApiAdapter(impl, interval=0)
    with pytest.raises(OneBotApiError) as ei:
        await api.get_group_fs_info("g1")
    assert ei.value.kind == OneBotErrorKind.REMOTE_ERROR
    assert api.capability("get_group_file_system_info") == CapabilityState.UNKNOWN  # 新语义：不标记


@pytest.mark.asyncio
async def test_local_error_passthrough_no_broken():
    """本地环境态（无 bot 上下文）：穿透原 kind，不标记 BROKEN、不退避（日志污染修复）。"""
    from core.domain.enums import OneBotErrorKind

    async def impl(action, **params):
        raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, action, "no onebot bot")

    api = NapCatApiAdapter(impl, interval=0)
    with pytest.raises(OneBotApiError) as ei:
        await api.get_group_fs_info("g1")
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert api.capability("get_group_file_system_info") == CapabilityState.UNKNOWN  # 未污染
    # 第二次调用仍穿透（无 backoff 拦截）
    with pytest.raises(OneBotApiError) as ei2:
        await api.get_group_fs_info("g1")
    assert ei2.value.kind == OneBotErrorKind.LOCAL_ERROR


@pytest.mark.asyncio
async def test_success_marks_supported():
    calls = []
    impl = await _call_impl(calls=calls)
    api = NapCatApiAdapter(impl, interval=0)
    fl = await api.list_group_root("g1")
    assert isinstance(fl, GroupFileList)
    assert api.capability("get_group_root_files") == CapabilityState.SUPPORTED


@pytest.mark.asyncio
async def test_limiter_min_interval():
    limiter = IntervalLimiter(interval=0.05, min_interval=0.01)
    t0 = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - t0 >= 0.05 - 0.01  # 至少间隔 50ms 容差


@pytest.mark.asyncio
async def test_adapter_rate_limits_calls():
    calls = []
    impl = await _call_impl(calls=calls)
    api = NapCatApiAdapter(impl, interval=0.05)
    await api.list_group_root("g1")
    await api.list_group_root("g1")
    assert len(calls) == 2
    # list_group_root → get_group_root_files 是读类，mult=0.4（20ms @ interval=50ms）
    assert calls[1][1] - calls[0][1] >= 0.015


# ---------- 事件索引容错 ----------

@pytest.mark.asyncio
async def test_index_event_ignores_non_upload(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    svc = ResourceSyncService(api, store)
    assert await svc.index_event({"notice_type": "group_increase"}) is False
    assert await svc.index_event({}) is False
    await store.close()


# ---------- 任务取消 → sync_logs 终态 ----------

@pytest.mark.asyncio
async def test_cancel_sync_writes_cancelled_state(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()

    class BlockingApi(FakeOneBotApi):
        async def list_group_root(self, group_id):
            gate = getattr(self, "gate", None)
            if gate:
                await gate.wait()
            return GroupFileList(group_id=group_id, files=[], folders=[])

    api = BlockingApi(tree={None: ([], [])})
    api.gate = asyncio.Event()
    svc = ResourceSyncService(api, store)
    lock = asyncio.Lock()
    task = asyncio.create_task(svc.run_full_sync("g1", lock))
    await asyncio.sleep(0.1)  # 让任务进入阻塞
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # sync_logs 终态必须为 cancelled（DoD 取消约定 / docs/06 §4）
    conn = sqlite3.connect(tmp_path / "meta.db")
    row = conn.execute(
        "SELECT status, complete FROM sync_logs WHERE group_id='g1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == SyncStatus.CANCELLED.value
    await store.close()