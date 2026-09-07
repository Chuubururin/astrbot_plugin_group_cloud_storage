"""状态筛选契约（N-02，2026-09-01 ADR-0008）：
files?status=netdisk|album|essence|none 派生过滤（SQL 层）+ items[].store_status 投影。

以 SqliteMetaStore.query_resources 的 ResourceQuery.store_status 过滤为契约锚点；
webapi 层投影（list_archived_done_ids）由测试存储方法联动验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    yield store
    await store.close()


async def _seed(store):
    """三行资源：一个普通文件、一个相册、一个精华；其中一个普通文件已归档 out+done。"""
    f0 = Resource(group_id="g1", type=ResourceType.FILE, name="a.docx", source_ref="f1",
                  size=10, busid=102, created_at=10)
    f1 = Resource(group_id="g1", type=ResourceType.FILE, name="b.mp4", source_ref="f2",
                  size=20, busid=102, created_at=20)
    a0 = Resource(group_id="g1", type=ResourceType.ALBUM, name="旅行相册", source_ref="a1",
                  size=30, busid=102, created_at=30,
                  meta={"album_id": "A1"})
    e0 = Resource(group_id="g1", type=ResourceType.ESSENCE, name="重要通知", source_ref="e1",
                  size=40, busid=102, created_at=40)
    await store.upsert_resources([f0, f1, a0, e0])
    page = await store.query_resources(ResourceQuery(group_id="g1", type=None, page_size=20))
    by_name = {it.name: it for it in page.items}
    # b.mp4 已归档 out+done → 「在网盘」
    await store.upsert_archive_map({
        "resource_id": by_name["b.mp4"].id,
        "group_id": "g1",
        "task_id": "t1",
        "remote_path": "/g1/b.mp4",
        "direction": "out",
        "state": "done",
        "updated_at": "2026-09-01T00:00:00+00:00",
    })
    return by_name


async def _names(store, rq) -> list[str]:
    page = await store.query_resources(rq)
    return sorted(it.name for it in page.items)


@pytest.mark.asyncio
async def test_status_none_excludes_archived_and_kinds(env):
    store = env
    await _seed(store)
    names = await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="none", page_size=20))
    assert names == ["a.docx"]  # b.mp4 在网盘、相册/精华为资源类型段


@pytest.mark.asyncio
async def test_status_netdisk_only_archived_done(env):
    store = env
    await _seed(store)
    names = await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="netdisk", page_size=20))
    assert names == ["b.mp4"]


@pytest.mark.asyncio
async def test_status_album_and_essence_kind_segments(env):
    store = env
    await _seed(store)
    assert await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="album", page_size=20)) == ["旅行相册"]
    assert await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="essence", page_size=20)) == ["重要通知"]


@pytest.mark.asyncio
async def test_status_empty_is_no_filter(env):
    store = env
    await _seed(store)
    names = await _names(store, ResourceQuery(group_id="g1", type=None, page_size=20))
    assert len(names) == 4


@pytest.mark.asyncio
async def test_list_archived_done_ids_batch(env):
    store = env
    by_name = await _seed(store)
    ids = [it.id for it in by_name.values()]
    done = await store.list_archived_done_ids(ids, direction="out")
    assert done == {by_name["b.mp4"].id}
    # 空输入幂等
    assert await store.list_archived_done_ids([]) == set()