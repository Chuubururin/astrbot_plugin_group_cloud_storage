"""分模块独立标签契约（W-9，2026-09-01 ADR-0008 N-04/N-05）：
- tag_cloud(kind) 隔离聚合（相册/精华/全局各自独立标签云，不复用统一标签语义）；
- files?kind=album/essence 响应携带模块隔离标签云。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402


def _res(t, name, tags, source_ref):
    return Resource(
        group_id="g1", type=t, name=name, source_ref=source_ref, size=1,
        busid=1, created_at=1, tags=tags, status=ResourceStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_tag_cloud_kind_isolated(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_resources([
        _res(ResourceType.FILE, "f.txt", ["全局标签", "共享"], "f1"),
        _res(ResourceType.ALBUM, "旅行相册", ["相册专用", "共享"], "a1"),
        _res(ResourceType.ESSENCE, "公告", ["精华专用"], "e1"),
    ])
    global_cloud = await store.tag_cloud(None)
    album_cloud = await store.tag_cloud("album")
    essence_cloud = await store.tag_cloud("essence")
    gtags = {t["tag"]: t["count"] for t in global_cloud}
    atags = {t["tag"]: t["count"] for t in album_cloud}
    etags = {t["tag"]: t["count"] for t in essence_cloud}
    # 全局 = 全类型聚合
    assert gtags.get("共享") == 2 and gtags.get("全局标签") == 1
    assert gtags.get("相册专用") == 1 and gtags.get("精华专用") == 1
    # 相册隔离：仅相册资源标签
    assert "相册专用" in atags and "共享" in atags
    assert "精华专用" not in atags
    # 精华隔离
    assert "精华专用" in etags
    assert "相册专用" not in etags
    await store.close()


@pytest.mark.asyncio
async def test_tag_cloud_deleted_excluded(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_resources([
        _res(ResourceType.FILE, "a.txt", ["保留"], "f1"),
        _res(ResourceType.FILE, "b.txt", ["移除"], "f2"),
    ])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10
        )
    )
    gone = next(it for it in page.items if it.name == "b.txt")
    await store.update_resource_fields(gone.id, status=ResourceStatus.DELETED.value)
    cloud = {t["tag"] for t in await store.tag_cloud(None)}
    assert "保留" in cloud and "移除" not in cloud
    await store.close()
