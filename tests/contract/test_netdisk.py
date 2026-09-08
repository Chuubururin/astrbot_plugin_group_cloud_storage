"""NetdiskService 契约测试（ADR-0004 / N4）：浏览登记 / 深度索引 / 直链 / 任务路由。

对齐 16 清单 §9 N4 与 HL-14（浏览登记 B 类、深度索引 C 类手动任务）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.openlist import NetFile  # noqa: E402
from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.config import PluginConfig  # noqa: E402
from core.application.netdisk import NetdiskService  # noqa: E402
from tests.fixtures.fake_openlist import FakeOpenListClient  # noqa: E402


def _nf(name, size=10, is_dir=False):
    return NetFile(name=name, size=size, is_dir=is_dir, modified="")


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    client = FakeOpenListClient()
    config = PluginConfig({"type_ext_overrides": {".xyz": "video"}})
    queue = SimpleNamespace(
        publish=lambda ev: published.append(ev),
        submit=None,
    )
    published: list[dict] = []
    netdisk = NetdiskService(client, store, config, queue)
    yield SimpleNamespace(store=store, client=client, netdisk=netdisk,
                          published=published, queue=queue)
    await store.close()


@pytest.mark.asyncio
async def test_browse_registers_and_merges_meta(env):
    ns = env
    ns.client.files["/"] = [_nf("a.pdf", 100), _nf("b.xyz", 20), _nf("sub", 0, True)]
    data = await ns.netdisk.browse("/", 1, 50)
    # 浏览即登记（B 类）：当前目录三条全部入表
    metas = await ns.store.get_netdisk_meta("/")
    assert len(metas) == 3
    by_path = {m["remote_path"]: m for m in metas}
    assert "/a.pdf" in by_path and "/sub/" in by_path
    # CT-9 分类（2026-09-01 13 类：pdf 独立类；含扩展名覆盖：.xyz -> video）
    assert by_path["/a.pdf"]["type"] == "pdf"
    assert by_path["/b.xyz"]["type"] == "video"
    assert by_path["/sub/"]["type"] == "folder"
    # 响应合并标记字段
    item = {i["name"]: i for i in data["items"]}
    assert item["a.pdf"]["type"] == "pdf"
    assert item["a.pdf"]["indexed_at"] == ""
    # 再次浏览：幂等（不重复插入、不覆盖标注）
    await ns.store.set_netdisk_tags("/a.pdf", "重要")
    await ns.netdisk.browse("/", 1, 50)
    metas = await ns.store.get_netdisk_meta("/")
    tags = {m["remote_path"]: m["tags"] for m in metas}
    assert tags["/a.pdf"] == "重要"


@pytest.mark.asyncio
async def test_browse_pagination(env):
    ns = env
    ns.client.files["/big"] = [_nf(f"f{i:03d}.bin") for i in range(120)]
    page1 = await ns.netdisk.browse("/big", 1, 50)
    page3 = await ns.netdisk.browse("/big", 3, 50)
    assert len(page1["items"]) == 50 and page1["has_more"] is True
    assert len(page3["items"]) == 20 and page3["has_more"] is False


@pytest.mark.asyncio
async def test_deep_index_recursive_and_progress(env):
    ns = env
    ns.client.files["/"] = [_nf("root.bin"), _nf("dir1", 0, True)]
    ns.client.files["/dir1/"] = [_nf("inner.mp4"), _nf("dir2", 0, True)]
    ns.client.files["/dir1/dir2/"] = [_nf("deep.xyz")]

    async def _handler(op):
        await ns.netdisk.handle_index(op)

    _seen_ops: list = []

    class _Op:
        kind = "netdisk_index"
        target = "/"
        payload = {"path": "/"}
        task_id = "idx1"

    await _handler(_Op())
    metas = await ns.store.get_netdisk_meta("/")
    paths = {m["remote_path"] for m in metas}
    assert "/root.bin" in paths
    assert "/dir1/inner.mp4" in paths
    assert "/dir1/dir2/deep.xyz" in paths
    # indexed_at 仅文件回填
    indexed = {m["remote_path"]: m["indexed_at"] for m in metas}
    assert indexed["/root.bin"] and indexed["/dir1/dir2/deep.xyz"]
    assert not indexed["/dir1/"]
    # 进度事件发布（SSE 契约：kind=netdisk_index）
    assert any(ev.get("kind") == "netdisk_index" for ev in ns.published)


@pytest.mark.asyncio
async def test_direct_link_memory_only(env):
    ns = env
    url = await ns.netdisk.direct_link("/g/a.bin")
    assert url.startswith("http://openlist.test/dl")
    # 直链不落库（REQ-06/HL-07）
    metas = await ns.store.get_netdisk_meta("/g/")
    assert all("url" not in (m or {}) for m in metas)
