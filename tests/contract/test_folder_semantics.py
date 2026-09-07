"""单级文件夹语义契约（W-5.2，2026-09-01 ADR-0008 N-03）：
群文件文件夹只有一级——资源在根目录或某个一级文件夹；
folder 查询参数（''=全部 / '__root__'=仅根 / 名称=匹配 folder_name）与三通道
（目录行导航 / 面包屑 / ../ 返回上级）共用同一契约。

本契约以 store 层 folder 查询 + folders 列表为锚点（webapi 直传参数，
前端三通道均为同一 folder 状态的读/写）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    yield store
    await store.close()


async def _seed(store):
    rows = [
        Resource(group_id="g1", type=ResourceType.FILE, name="root.txt",
                 source_ref="r1", size=1, busid=1, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="docs.txt",
                 source_ref="r2", size=2, busid=1, created_at=2, folder_name="文档"),
        Resource(group_id="g1", type=ResourceType.FILE, name="docs2.txt",
                 source_ref="r3", size=3, busid=1, created_at=3, folder_name="文档"),
        Resource(group_id="g1", type=ResourceType.FILE, name="imgs.txt",
                 source_ref="r4", size=4, busid=1, created_at=4, folder_name="图片"),
    ]
    await store.upsert_resources(rows)


async def _names(store, rq) -> list[str]:
    page = await store.query_resources(rq)
    return sorted(it.name for it in page.items)


@pytest.mark.asyncio
async def test_folder_root_only(env):
    store = env
    await _seed(store)
    names = await _names(store, ResourceQuery(group_id="g1", folder="__root__", page_size=20))
    assert names == ["root.txt"]


@pytest.mark.asyncio
async def test_folder_by_name_single_level(env):
    store = env
    await _seed(store)
    names = await _names(store, ResourceQuery(group_id="g1", folder="文档", page_size=20))
    assert names == ["docs.txt", "docs2.txt"]
    # 空 folder = 全部（不限制目录）
    all_names = await _names(store, ResourceQuery(group_id="g1", page_size=20))
    assert len(all_names) == 4


@pytest.mark.asyncio
async def test_folders_list_is_flat(env):
    """目录清单实体为扁平一级（list_folders_detail 无 parent 层级展开语义）。"""
    store = env
    await _seed(store)
    folders = await store.list_folders_detail("g1")
    # 未扫描前无目录实体（扫描维护）；此处验证查询契约返回 dict 结构
    assert isinstance(folders, list)
    for f in folders:
        assert "folder_name" in f and "folder_id" in f
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                 source_ref="r9", size=1, busid=1, created_at=9, folder_name="新建目录"),
    ])
    # folder_name 覆盖有文件的目录也会出现在 directories 语义中（resources 侧）
    page = await store.query_resources(
        ResourceQuery(group_id="g1", folder="新建目录", page_size=20)
    )
    assert [it.name for it in page.items] == ["a.txt"]