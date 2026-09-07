"""凋零差分对账契约（W-8 / D-4，2026-09-01）：
- run_diff_sync：根+一级文件夹列表 → upsert 增补；云端消失条目软删凋零；
- 缺席（complete=False）→ 冻结，不凋零（保守阈值的冻结窗口）；
- 全量 run_full_sync 仍可用（手动例外）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.domain.sync import ResourceQuery  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    yield tmp_path, store, api, sync
    await store.close()


@pytest.mark.asyncio
async def test_diff_sync_adds_new_and_withers_missing(env):
    """增补平衡：新文件落库 + 云端消失条目剔除（凋零）。"""
    tmp_path, store, api, sync = env
    api.tree = {
        None: ([{"file_id": "f1", "name": "a.txt", "size": 10, "busid": 102}], [{"folder_id": "d1", "name": "文档"}]),
        "d1": ([{"file_id": "f2", "name": "b.txt", "size": 20, "busid": 102}], []),
    }
    result = await sync.run_diff_sync("g1", asyncio.Lock())
    assert result.ok and result.complete
    assert result.files_found == 2 and result.files_indexed >= 2
    page = await store.query_resources(
        ResourceQuery(group_id="g1", type=None, page_size=20)
    )
    assert {it.name for it in page.items} == {"a.txt", "b.txt"}

    # 云端 b.txt 消失 → 差分凋零
    api.tree = {None: ([{"file_id": "f1", "name": "a.txt", "size": 10, "busid": 102}], [{"folder_id": "d1", "name": "文档"}]), "d1": ([], [])}
    result2 = await sync.run_diff_sync("g1", asyncio.Lock())
    assert result2.ok
    assert result2.files_removed >= 1
    page = await store.query_resources(
        ResourceQuery(group_id="g1", type=None, page_size=20)
    )
    active = [it for it in page.items if it.type == "file"]
    assert {it.name for it in active} == {"a.txt"}


@pytest.mark.asyncio
async def test_diff_sync_frozen_when_cloud_absent(env):
    """缺席冻结：api 抛错 → 不凋零（保守窗口），本地条目保留。"""
    tmp_path, store, api, sync = env
    api.tree = {None: ([{"file_id": "f1", "name": "a.txt", "size": 10, "busid": 102}], [])}
    r1 = await sync.run_diff_sync("g1", asyncio.Lock())
    assert r1.ok

    class _Broken:
        async def list_group_root(self, group_id):
            raise RuntimeError("cloud absent")

        async def list_group_folder(self, group_id, folder_id):
            raise RuntimeError("cloud absent")

        async def list_group_members(self, group_id):
            raise RuntimeError("cloud absent")

    sync.api = _Broken()
    r2 = await sync.run_diff_sync("g1", asyncio.Lock())
    assert not r2.ok and not r2.complete
    page = await store.query_resources(
        ResourceQuery(group_id="g1", type=None, page_size=20)
    )
    active = [it for it in page.items if it.type == "file"]
    assert {it.name for it in active} == {"a.txt"}  # 未被误凋零


@pytest.mark.asyncio
async def test_diff_sync_respects_lock_mutex(env):
    """同群互斥：锁占用时拒绝执行第二条差分（AC8 语义延伸）。"""
    tmp_path, store, api, sync = env
    lock = asyncio.Lock()
    await lock.acquire()
    try:
        r = await sync.run_diff_sync("g1", lock)
        assert not r.ok and r.error
    finally:
        lock.release()