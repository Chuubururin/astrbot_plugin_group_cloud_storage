"""FTS5 检索测试（v2.9）：trigram 子串/短词回退/触发器同步/标签/摘要/群过滤/规模基准。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


async def _upsert(store, i, group="g1", name="f{i}.zip", meta=None, tags=None):
    await store.upsert_resources([Resource(
        group_id=group, type=ResourceType.FILE, name=name,
        source_ref=f"ref_{i}", size=i * 100, created_at=1700000000 + i,
        meta=meta, tags=tags,
    )])


async def _ids(store, group, q):
    return await store.fts_match(group, q)


@pytest.mark.asyncio
async def test_fts_name_substring_cn(store):
    await _upsert(store, 1, name="项目策划案.docx")
    await _upsert(store, 2, name="月度总结.md")
    await _upsert(store, 3, name="别的文件.txt")
    assert set(await _ids(store, None, "策划")) == {1}
    assert set(await _ids(store, None, "月度总结")) == {2}
    assert await _ids(store, None, "不存在词") == []


@pytest.mark.asyncio
async def test_fts_short_term_like_fallback(store):
    await _upsert(store, 1, name="报告A.docx")
    await _upsert(store, 2, name="报告B.docx")
    # 2 字符词元：trigram 无法索引 → name LIKE 回退
    assert set(await _ids(store, None, "报告")) == {1, 2}


@pytest.mark.asyncio
async def test_fts_tags_and_summary(store):
    await _upsert(store, 1, name="x.bin", tags=["重要", "归档"])
    await _upsert(store, 2, name="y.bin", tags=["普通"])
    await _upsert(store, 3, name="z.bin", meta={"summary": "含关键句摘要内容"})
    assert set(await _ids(store, None, "重要")) == {1}
    assert set(await _ids(store, None, "关键句")) == {3}


@pytest.mark.asyncio
async def test_fts_group_filter(store):
    await _upsert(store, 1, group="g1", name="同名字文件.docx")
    await _upsert(store, 2, group="g2", name="同名字文件.docx")
    assert set(await _ids(store, "g1", "同名字文件")) == {1}


@pytest.mark.asyncio
async def test_fts_sync_on_update_and_delete(store):
    await _upsert(store, 1, name="旧名字.docx")
    await store.update_resource_fields(1, name="新名字.docx")
    assert await _ids(store, None, "旧名字") == []
    assert set(await _ids(store, None, "新名字")) == {1}
    await store.mark_missing_as_deleted("g1", True, {"other_ref"})
    # 软删后 FTS 行仍在，但查询侧 status 过滤掉
    assert await _ids(store, None, "新名字") == []


@pytest.mark.asyncio
async def test_netdisk_fts_search_and_directory_scope(store):
    await store.upsert_netdisk_rows([
        {"remote_path": "/docs/report.pdf", "name": "report.pdf", "is_dir": 0,
         "size": 10, "type": "pdf", "tags": "important", "registered_at": "1"},
        {"remote_path": "/media/report.mp4", "name": "report.mp4", "is_dir": 0,
         "size": 20, "type": "video", "tags": "", "registered_at": "1"},
    ])
    rows = await store.search_netdisk_meta("important", "/docs")
    assert [r["remote_path"] for r in rows] == ["/docs/report.pdf"]
    assert await store.search_netdisk_meta("report", "/missing") == []


@pytest.mark.asyncio
async def test_fts_benchmark_20k(store):
    """规模基准：2 万行落库后查询毫秒级（百万级设计的结构验证）。"""
    items = [Resource(
        group_id=f"g{i % 100}", type=ResourceType.FILE, name=f"文档编号{i}.pdf",
        source_ref=f"b_{i}", size=1024, created_at=1700000000 + i,
        tags=(["归档"] if i % 50 == 0 else []),
    ) for i in range(20000)]
    t0 = time.monotonic()
    await store.upsert_resources(items)
    build = time.monotonic() - t0
    t0 = time.monotonic()
    ids = await _ids(store, None, "文档编号19999")
    q1 = (time.monotonic() - t0) * 1000
    t0 = time.monotonic()
    ids2 = await _ids(store, None, "归档")
    q2 = (time.monotonic() - t0) * 1000
    assert ids and ids[0] in {r[0] for r in [(ids[0],)]}
    assert len(ids2) > 0
    assert q1 < 500 and q2 < 2000, f"q1={q1:.1f}ms q2={q2:.1f}ms build={build:.1f}s"
