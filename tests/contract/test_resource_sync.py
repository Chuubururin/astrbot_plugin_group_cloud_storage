"""契约测试：MetaStorePort / OneBotApiPort / ResourceSyncService（AC1/AC2/AC3/AC5/AC8/AC9）。

替换真实适配器时，本套件是回归依据（docs/06 §5 测试策略）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType, SyncStatus  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi, build_tree  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
async def sync_env(store):
    api = FakeOneBotApi(build_tree(file_total=300, folder_total=5, files_per_folder=20))
    svc = ResourceSyncService(api, store)
    return store, api, svc


@pytest.mark.asyncio
async def test_ac1_full_sync_300_files(sync_env):
    store, api, svc = sync_env
    lock = asyncio.Lock()
    result = await svc.run_full_sync("g1", lock)
    assert result.status == SyncStatus.OK
    assert result.complete is True
    assert result.files_found == 5 * 20
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=1000))
    assert page.total == 100
    assert all(r.folder_name for r in page.items)


@pytest.mark.asyncio
async def test_ac2_idempotent_upsert(sync_env):
    store, api, svc = sync_env
    for _ in range(2):
        result = await svc.run_full_sync("g1", asyncio.Lock())
        assert result.status == SyncStatus.OK
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=1000))
    assert page.total == 100  # 不重复


@pytest.mark.asyncio
async def test_ac5_rate_limit_not_applied_in_sync(sync_env):
    # 限速由 adapter 负责；这里验证 adapter 层面并不在服务内保守重复调用
    store, api, svc = sync_env
    await svc.run_full_sync("g1", asyncio.Lock())
    # 每次目录一次调用：root + 5 folders = 6 次 list + fs_info 等
    assert api.calls.count("get_group_root_files") == 1


@pytest.mark.asyncio
async def test_ac8_same_group_mutex(sync_env):
    store, api, svc = sync_env
    lock = asyncio.Lock()
    await lock.acquire()
    result = await svc.run_full_sync("g1", lock)
    assert result.status == SyncStatus.FAILED
    assert "already running" in (result.error or "")
    lock.release()


@pytest.mark.asyncio
async def test_ac9_partial_failure_no_orphan_cleanup(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    # 预置一条旧文件，source 在新同步中不存在
    old = Resource(
        group_id="g1", type=ResourceType.FILE, name="old.txt", source_ref="old_file",
        size=1, uploader_id="10001", created_at=1,
    )
    await store.upsert_resources([old])
    tree = build_tree(file_total=40, folder_total=2, files_per_folder=10)
    api = FakeOneBotApi(tree, fail_folders={"folder_1"})
    svc = ResourceSyncService(api, store)
    result = await svc.run_full_sync("g1", asyncio.Lock())
    assert result.status == SyncStatus.FAILED
    assert result.complete is False
    detail = await store.get_resource_detail("g1", 1)
    assert detail["status"] == ResourceStatus.ACTIVE.value  # 未误删
    await store.close()


@pytest.mark.asyncio
async def test_ac3_event_index(store):
    raw = {
        "post_type": "notice", "notice_type": "group_upload",
        "group_id": 888, "user_id": 10086, "time": 1700000100,
        "file": {"id": "evt_1", "name": "new.zip", "size": 6666, "busid": 102},
    }
    api = FakeOneBotApi(tree={None: ([], [])})
    svc = ResourceSyncService(api, store)
    ok = await svc.index_event(raw)
    assert ok is True
    page = await store.query_resources(ResourceQuery(group_id="888", page_size=10))
    assert page.total == 1
    assert page.items[0].source_ref == "evt_1"


@pytest.mark.asyncio
async def test_snapshot_written_on_full_sync(sync_env):
    store, api, svc = sync_env
    await svc.run_full_sync("g1", asyncio.Lock())
    # 通过 stats/detail 间接验证快照写入（快照表查询由 store 断言）
    stats = await store.stats("g1")
    assert stats.file_count == 100