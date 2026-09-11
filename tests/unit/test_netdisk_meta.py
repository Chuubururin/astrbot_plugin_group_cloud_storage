"""NetdiskMeta 存储测试（schema v14，ADR-0004）：幂等登记 / 标记 / 索引回填。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    st = SqliteMetaStore(tmp_path / "meta.db")
    await st.init()
    yield st
    await st.close()


def _row(path, name="a.bin", is_dir=0, size=10, rtype="other"):
    from datetime import datetime, timezone
    return {
        "remote_path": path, "name": name, "is_dir": is_dir, "size": size,
        "type": rtype, "tags": "", "registered_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_upsert_idempotent_no_overwrite(store):
    rows = [_row("/g1/a.bin"), _row("/g1/b.mp4", "b.mp4", size=20, rtype="video")]
    inserted = await store.upsert_netdisk_rows(rows)
    assert inserted == 2
    # 重复浏览：幂等，不新增
    inserted2 = await store.upsert_netdisk_rows(rows)
    assert inserted2 == 0
    # 人工标注后再次登记：标注不被覆盖（ADR-0004 决策 2）
    await store.set_netdisk_tags("/g1/a.bin", "重要")
    await store.upsert_netdisk_rows([_row("/g1/a.bin")])
    metas = await store.get_netdisk_meta("/g1/")
    tags = {m["remote_path"]: m["tags"] for m in metas}
    assert tags["/g1/a.bin"] == "重要"


@pytest.mark.asyncio
async def test_get_by_prefix_and_mark_indexed(store):
    await store.upsert_netdisk_rows([
        _row("/g1/a.bin"), _row("/g1/sub/c.bin", "c.bin"), _row("/g2/d.bin", "d.bin"),
    ])
    metas = await store.get_netdisk_meta("/g1/")
    paths = {m["remote_path"] for m in metas}
    assert paths == {"/g1/a.bin", "/g1/sub/c.bin"}
    await store.mark_netdisk_indexed(["/g1/a.bin"])
    metas = await store.get_netdisk_meta("/g1/")
    indexed = {m["remote_path"]: m["indexed_at"] for m in metas}
    assert indexed["/g1/a.bin"] and not indexed["/g1/sub/c.bin"]


@pytest.mark.asyncio
async def test_set_tags_empty_allowed(store):
    await store.upsert_netdisk_rows([_row("/g/x.bin", "x.bin")])
    await store.set_netdisk_tags("/g/x.bin", "")
    metas = await store.get_netdisk_meta("/g/")
    assert metas[0]["tags"] == ""


@pytest.mark.asyncio
async def test_set_tags_unregistered_path_survives_registration(store):
    # 对未登记路径打标：upsert 占位行，标签不丢（链⑬ 真机复现）
    await store.set_netdisk_tags("/g/fresh.bin", "孤儿标签")
    metas = await store.get_netdisk_meta("/g/")
    assert [m["tags"] for m in metas] == ["孤儿标签"]
    # 登记同路径：幂等不覆盖标注
    await store.upsert_netdisk_rows([_row("/g/fresh.bin", "fresh.bin")])
    metas = await store.get_netdisk_meta("/g/")
    assert len(metas) == 1
    assert metas[0]["tags"] == "孤儿标签"
    assert metas[0]["name"] == "fresh.bin"
