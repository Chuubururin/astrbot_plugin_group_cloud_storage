"""状态筛选契约（N-02，2026-09-01 ADR-0008；2026-09-07 语义修订）：
files?status=netdisk|album|essence|none 派生过滤（SQL 层）+ items[].store_status 投影。

语义：状态筛选是**交叉存在性判定**，不是类型切换——Files tab 恒为 type='file'
行，「在网盘」按 archive_map(direction=out, state=done) id 关联；「在相册/在精华」
按同群同名跨类型资源存在（转存管线保留原文件名，文件名兜底为唯一关联通道，
文件 id 关联不可用）；「未下载」= 三者皆不命中。相册/精华行本身是其他 tab 的
内容，不再因 store_status 被本筛选返回。

以 SqliteMetaStore.query_resources 的 ResourceQuery.store_status 过滤为契约锚点；
webapi 层投影（list_archived_done_ids / find_cross_store_copies）由测试存储方法
联动验证。
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
    """五行资源：两个普通文件（其一已归档）、一个相册、一个精华、
    一个与相册同名的普通文件（交叉判定命中）。"""
    f0 = Resource(group_id="g1", type=ResourceType.FILE, name="a.docx", source_ref="f1",
                  size=10, busid=102, created_at=10)
    f1 = Resource(group_id="g1", type=ResourceType.FILE, name="b.mp4", source_ref="f2",
                  size=20, busid=102, created_at=20)
    a0 = Resource(group_id="g1", type=ResourceType.ALBUM, name="旅行相册", source_ref="a1",
                  size=30, busid=102, created_at=30,
                  meta={"album_id": "A1"})
    e0 = Resource(group_id="g1", type=ResourceType.ESSENCE, name="重要通知", source_ref="e1",
                  size=40, busid=102, created_at=40)
    f2 = Resource(group_id="g1", type=ResourceType.FILE, name="旅行相册", source_ref="f3",
                  size=50, busid=102, created_at=50)
    await store.upsert_resources([f0, f1, a0, e0, f2])
    page = await store.query_resources(ResourceQuery(group_id="g1", type=None, page_size=20))
    # 同名跨类型行并存：按 (name, type) 键控，避免字典覆盖丢行
    by_key = {(it.name, it.type): it for it in page.items}
    # b.mp4 已归档 out+done → 「在网盘」
    await store.upsert_archive_map({
        "resource_id": by_key[("b.mp4", "file")].id,
        "group_id": "g1",
        "task_id": "t1",
        "remote_path": "/g1/b.mp4",
        "direction": "out",
        "state": "done",
        "updated_at": "2026-09-01T00:00:00+00:00",
    })
    return by_key


async def _names(store, rq) -> list[str]:
    page = await store.query_resources(rq)
    return sorted(it.name for it in page.items)


async def _file_names(store, rq) -> list[str]:
    """Files tab 语义：type='file' 恒定 + store_status 交叉过滤。"""
    rq.type = "file"
    page = await store.query_resources(rq)
    return sorted(it.name for it in page.items)


@pytest.mark.asyncio
async def test_status_none_excludes_archived_and_cross_copies(env):
    store = env
    await _seed(store)
    # 未下载 = 非网盘归档、且同群无同名相册/精华；a.docx 唯一命中
    names = await _file_names(store, ResourceQuery(
        group_id="g1", type="file", store_status="none", page_size=20))
    assert names == ["a.docx"]


@pytest.mark.asyncio
async def test_status_none_no_type_filter_excludes_cross_copies(env):
    store = env
    await _seed(store)
    # type=None（全文检索等通道）：交叉命中的行被排除——「旅行相册」文件行
    # 因同群同名相册被排除；相册行同名但同类交叉判定不命中，存活；
    # b.mp4 因归档记录被排除（netdisk 判定参与 none）。
    names = await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="none", page_size=20))
    assert names == ["a.docx", "旅行相册", "重要通知"]


@pytest.mark.asyncio
async def test_status_netdisk_only_archived_done(env):
    store = env
    await _seed(store)
    names = await _file_names(store, ResourceQuery(
        group_id="g1", type="file", store_status="netdisk", page_size=20))
    assert names == ["b.mp4"]


@pytest.mark.asyncio
async def test_status_album_cross_reference_not_type_rows(env):
    store = env
    await _seed(store)
    # 在相册 = 群文件中同群存在同名相册的行（旅行相册 文件行），
    # 不再返回相册行本身
    names = await _file_names(store, ResourceQuery(
        group_id="g1", type="file", store_status="album", page_size=20))
    assert names == ["旅行相册"]


@pytest.mark.asyncio
async def test_status_album_without_type_filter_returns_cross_files(env):
    store = env
    await _seed(store)
    # type=None 时同样只回交叉命中的行（含文件行），不含相册行本身
    names = await _names(store, ResourceQuery(
        group_id="g1", type=None, store_status="album", page_size=20))
    assert names == ["旅行相册"]


@pytest.mark.asyncio
async def test_status_essence_cross_reference_not_type_rows(env):
    store = env
    await _seed(store)
    # 在精华 = 同群存在同名精华的群文件行；本组无同名 → 空
    names = await _file_names(store, ResourceQuery(
        group_id="g1", type="file", store_status="essence", page_size=20))
    assert names == []


@pytest.mark.asyncio
async def test_status_empty_is_no_filter(env):
    store = env
    await _seed(store)
    names = await _file_names(store, ResourceQuery(
        group_id="g1", type="file", page_size=20))
    assert names == ["a.docx", "b.mp4", "旅行相册"]


@pytest.mark.asyncio
async def test_list_archived_done_ids_batch(env):
    store = env
    by_key = await _seed(store)
    ids = [it.id for it in by_key.values()]
    done = await store.list_archived_done_ids(ids, direction="out")
    assert done == {by_key[("b.mp4", "file")].id}
    # 空输入幂等
    assert await store.list_archived_done_ids([]) == set()


@pytest.mark.asyncio
async def test_find_cross_store_copies_name_linkage(env):
    store = env
    by_key = await _seed(store)
    rows = [(it.id, "g1", name) for (name, _t), it in by_key.items()]
    copies = await store.find_cross_store_copies(rows)
    # 「旅行相册」文件行命中同群同名相册；其余不命中
    assert copies["album"] == {by_key[("旅行相册", "file")].id}
    assert copies["essence"] == set()
    # 相册行自身不命中（排除自匹配）
    assert by_key[("旅行相册", "album")].id not in copies["album"]
    # 空输入幂等
    assert await store.find_cross_store_copies([]) == {"album": set(), "essence": set()}


@pytest.mark.asyncio
async def test_cross_store_copies_group_isolated(env):
    """同名但跨群不命中：交叉判定必须限定同群。"""
    store = env
    await _seed(store)
    f_other = Resource(group_id="g2", type=ResourceType.FILE, name="旅行相册",
                       source_ref="g2f1", size=1, busid=102, created_at=60)
    await store.upsert_resources([f_other])
    page = await store.query_resources(ResourceQuery(group_id="g2", type="file", page_size=20))
    target = page.items[0]
    copies = await store.find_cross_store_copies([(target.id, "g2", target.name)])
    assert copies["album"] == set()
