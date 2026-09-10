"""SqliteMetaStore 单元测试（Slice 0，docs/06 §5）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite.migrations import SCHEMA_VERSION as _SCHEMA_VERSION  # noqa: E402
from core.domain.enums import ResourceType, SyncKind, SyncStatus  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery, SyncLog, SyncResult  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


def _res(i: int, group: str = "g1") -> Resource:
    return Resource(
        group_id=group, type=ResourceType.FILE, name=f"f{i}.zip", source_ref=f"ref_{i}",
        size=i * 100, uploader_id="10001", uploader_name="Alice", busid=102,
        folder_id="dir1", folder_name="Docs", created_at=1700000000 + i,
    )


@pytest.mark.asyncio
async def test_init_idempotent(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    await s.init()  # 二次初始化不报错
    await s.close()


@pytest.mark.asyncio
async def test_upsert_idempotent(store):
    n1 = await store.upsert_resources([_res(1), _res(2)])
    n2 = await store.upsert_resources([_res(2)])  # 重复
    assert n1 == 2 and n2 >= 1
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=100))
    assert page.total == 2


@pytest.mark.asyncio
async def test_upsert_preserves_composition_meta(store):
    """回归（0/n 分卷不完整）：full_sync 重索引云端同一文件时，分卷父资源的
    meta（volumes/composition/total_sha256）必须保留——否则父资源丢失组合
    语义，前端不再显示分卷徽章、删除走普通分支留下孤儿 part。普通文件的
    meta 仍按 excluded 覆盖（同步是最终权威）。"""
    rid = "g1:file:ref_1"
    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name="f1.zip",
                source_ref="ref_1", size=100, created_at=1700000001,
                meta={"volumes": True, "compression": "zip-part",
                      "composition": {"kind": "volumes", "parts": 2}},
            )
        ]
    )
    # 同步重索引：同一 resource_id（group:file:source_ref），meta 为空
    resync = Resource(
        group_id="g1", type=ResourceType.FILE, name="f1.zip",
        source_ref="ref_1", size=100, uploader_id="10001", busid=102,
        created_at=1700000001,
    )
    await store.upsert_resources([resync])
    detail = await store.get_resource_by_resource_id(rid)
    meta = detail["meta"] or {}
    assert meta.get("volumes") is True
    assert (meta.get("composition") or {}).get("kind") == "volumes"
    # 普通文件（无 composition）：meta 仍被重索引覆盖
    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name="f2.zip",
                source_ref="ref_2", size=200, created_at=1700000002,
                meta={"custom": 1},
            )
        ]
    )
    resync2 = Resource(
        group_id="g1", type=ResourceType.FILE, name="f2.zip",
        source_ref="ref_2", size=200, uploader_id="10001", busid=102,
        created_at=1700000002,
    )
    await store.upsert_resources([resync2])
    detail2 = await store.get_resource_by_resource_id("g1:file:ref_2")
    assert detail2["meta"] == {}


@pytest.mark.asyncio
async def test_successor_row_inherits_composition_and_relinks_volumes(store):
    """回归（0/n 分卷不完整·复发路径）：NapCat file_id 是会话级句柄，重启后
    同一云端文件以新 file_id 重扫 → 新 resource_id 行（meta 空），携带
    composition 的旧行被 sweep 标 deleted，volumes 仍挂旧 parent —— 下载
    显示 0/n 分卷不完整。修复：upsert 时同 (group, name) 的已删 composition
    行把 meta 让渡给新行、volumes 重挂到新行、旧行硬删。"""
    from core.domain.sync import VolumeInfo

    old = Resource(
        group_id="g1", type=ResourceType.FILE, name="big.bin",
        source_ref="old_ref", size=1000, created_at=1700000001,
        meta={"volumes": True, "compression": "zip-part",
              "composition": {"kind": "volumes", "parts": 3}},
    )
    await store.upsert_resources([old])
    await store.insert_volumes([
        VolumeInfo(parent_resource_id="g1:file:old_ref", seq=1,
                   part_name="big.part01.zip", source_ref="p1", busid=1,
                   size=100, sha256="a" * 64, status="ready", upload_time=1,
                   group_id="g1"),
        VolumeInfo(parent_resource_id="g1:file:old_ref", seq=2,
                   part_name="big.part02.zip", source_ref="p2", busid=2,
                   size=100, sha256="b" * 64, status="ready", upload_time=2,
                   group_id="g1"),
    ])
    # 旧行被 sweep 软删（file_id 不再出现于云端清单）
    await store.mark_missing_as_deleted("g1", True, set())
    # 重启后重扫：同一逻辑文件以新 file_id 列出 → 新 resource_id 行
    fresh = Resource(
        group_id="g1", type=ResourceType.FILE, name="big.bin",
        source_ref="new_ref", size=1000, uploader_id="10001", busid=9,
        created_at=1700000002,
    )
    await store.upsert_resources([fresh])

    # 新行继承 composition 语义
    d = await store.get_resource_by_resource_id("g1:file:new_ref")
    meta = d["meta"] or {}
    assert meta.get("volumes") is True
    assert (meta.get("composition") or {}).get("kind") == "volumes"
    # volumes 重挂到新行：下载链路可取到全部分卷
    vols = await store.list_volumes("g1:file:new_ref")
    assert len(vols) == 2
    assert {v.part_name for v in vols} == {"big.part01.zip", "big.part02.zip"}
    # 旧行不复存在（身份唯一），0/2 不再出现
    assert await store.get_resource_by_resource_id("g1:file:old_ref") is None
    # 折叠条件恢复：part 行按 volumes 匹配（父行为新行）
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=50))
    names = {it.name for it in page.items}
    assert "big.bin" in names


@pytest.mark.asyncio
async def test_same_name_smaller_file_does_not_inherit_composition(store):
    """尺寸门（2026-09-10 线上 10879 案例）：同名新上传的更小文件不是旧
    组合文件的接续（云端原件在转换后已删除），不得继承 composition、
    不得重挂分卷。旧行保留、分卷仍挂旧行。"""
    from core.domain.sync import VolumeInfo

    old = Resource(
        group_id="g1", type=ResourceType.FILE, name="vol_test.bin",
        source_ref="old_ref", size=200000000, created_at=1700000001,
        meta={"volumes": True, "compression": "zip-part",
              "composition": {"kind": "volumes", "parts": 3}},
    )
    await store.upsert_resources([old])
    await store.insert_volumes([
        VolumeInfo(parent_resource_id="g1:file:old_ref", seq=1,
                   part_name="vol_test.part01of03.zip", source_ref="p1",
                   busid=1, size=100, sha256="a" * 64, status="ready",
                   upload_time=1, group_id=None),
    ])
    # 同名但小得多的新文件（旧组合原件 200MB，转换后云端已删）
    small = Resource(
        group_id="g1", type=ResourceType.FILE, name="vol_test.bin",
        source_ref="new_ref", size=2048, uploader_id="10001", busid=9,
        created_at=1700000002,
    )
    await store.upsert_resources([small])

    d = await store.get_resource_by_resource_id("g1:file:new_ref")
    assert (d["meta"] or {}) == {}
    assert await store.get_resource_by_resource_id("g1:file:old_ref") is not None
    vols = await store.list_volumes("g1:file:old_ref")
    assert len(vols) == 1
    # 更大（或同尺寸）的接续者仍然继承（覆盖旧路径不回归）
    big = Resource(
        group_id="g1", type=ResourceType.FILE, name="vol_test.bin",
        source_ref="big_ref", size=200000000, uploader_id="10001", busid=9,
        created_at=1700000003,
    )
    await store.upsert_resources([big])
    d2 = await store.get_resource_by_resource_id("g1:file:big_ref")
    assert (d2["meta"] or {}).get("volumes") is True
    assert await store.list_volumes("g1:file:big_ref")


@pytest.mark.asyncio
async def test_query_filter_keyword(store):
    await store.upsert_resources([_res(1), _res(2), _res(3)])
    page = await store.query_resources(
        ResourceQuery(group_id="g1", keyword="f2", page_size=10)
    )
    assert page.total == 1 and page.items[0].name == "f2.zip"


@pytest.mark.asyncio
async def test_detail_scoped_by_group(store):
    await store.upsert_resources([_res(1, group="g1"), _res(1, group="g2")])
    # g1 的 id=1 就是第一条
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id
    assert await store.get_resource_detail("g1", rid) is not None
    # 跨群查不到（AC10 防泄漏）
    assert await store.get_resource_detail("g2", rid) is None


@pytest.mark.asyncio
async def test_stats(store):
    await store.upsert_resources([_res(1), _res(2), _res(3)])
    st = await store.stats("g1")
    assert st.file_count == 3
    assert st.total_size == 600
    assert st.uploaders == 1
    assert st.by_folder and st.by_folder[0]["bytes"] == 600


@pytest.mark.asyncio
async def test_mark_missing_gated_by_complete(store):
    await store.upsert_resources([_res(1), _res(2)])
    # complete=False → 禁止清理
    n = await store.mark_missing_as_deleted("g1", False, {"ref_1"})
    assert n == 0
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 2  # 无变化
    # complete=True → 允许清理不在清单中的（ref_2 置 deleted）
    n = await store.mark_missing_as_deleted("g1", True, {"ref_1"})
    assert n == 1
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 1  # 仅 ref_1 仍 active


@pytest.mark.asyncio
async def test_mark_missing_handles_more_than_150_source_ids(store):
    """A source ID in a later batch must not be treated as missing."""
    await store.upsert_resources([_res(i) for i in range(151)])

    n = await store.mark_missing_as_deleted("g1", True, {f"ref_{i}" for i in range(151)})

    assert n == 0
    assert (await store.query_resources(ResourceQuery(group_id="g1", page_size=200))).total == 151


@pytest.mark.asyncio
async def test_mark_missing_empty_complete_listing_deletes_all_files(store):
    await store.upsert_resources([_res(1), _res(2)])

    n = await store.mark_missing_as_deleted("g1", True, set())

    assert n == 2
    assert (await store.query_resources(ResourceQuery(group_id="g1", page_size=10))).total == 0


@pytest.mark.asyncio
async def test_mark_missing_incomplete_listing_does_not_delete_files(store):
    await store.upsert_resources([_res(1), _res(2)])

    n = await store.mark_missing_as_deleted("g1", False, set())

    assert n == 0
    assert (await store.query_resources(ResourceQuery(group_id="g1", page_size=10))).total == 2


@pytest.mark.asyncio
async def test_query_sort_by_size_and_name(store):
    await store.upsert_resources([_res(1), _res(2), _res(3)])
    # 大小降序
    page = await store.query_resources(
        ResourceQuery(group_id="g1", page_size=10, sort_by="size", sort_dir="desc")
    )
    assert [it.name for it in page.items] == ["f3.zip", "f2.zip", "f1.zip"]
    # 名称升序
    page = await store.query_resources(
        ResourceQuery(group_id="g1", page_size=10, sort_by="name", sort_dir="asc")
    )
    assert [it.name for it in page.items] == ["f1.zip", "f2.zip", "f3.zip"]
    # 非法排序字段回退 id
    page = await store.query_resources(
        ResourceQuery(group_id="g1", page_size=10, sort_by="evil; DROP", sort_dir="desc")
    )
    assert page.total == 3


@pytest.mark.asyncio
async def test_upsert_does_not_resurrect_deleted(store):
    await store.upsert_resources([_res(1)])
    # 孤儿清理将其置 deleted
    await store.mark_missing_as_deleted("g1", True, {"other"})
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 0
    # 事件/同步再次 upsert 同一文件：不得复活（status 保留原值）
    await store.upsert_resources([_res(1)])
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 0  # 未复活


@pytest.mark.asyncio
async def test_sync_log_flow(store):
    log_id = await store.create_sync_log(
        SyncLog(group_id="g1", kind=SyncKind.FULL, start_at=1)
    )
    await store.finish_sync_log(
        log_id, SyncResult(status=SyncStatus.OK, files_found=5, files_indexed=4, complete=True)
    )

# ---------- v2 迁移与群管理（docs/09 §12） ----------

async def _build_v1_db(path: Path) -> None:
    """手工构造 v1 旧库（v1 建表 + 数据 + schema_version=1），验证增量迁移。"""
    import sqlite3

    conn = sqlite3.connect(path)
    # 只建 v1 的 groups（模拟旧库：仅旧列）
    conn.executescript(
        """
        CREATE TABLE resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id TEXT UNIQUE NOT NULL, group_id TEXT NOT NULL, type TEXT NOT NULL,
            name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, mime TEXT,
            uploader_id TEXT, uploader_name TEXT, source_ref TEXT NOT NULL, busid INTEGER,
            folder_id TEXT, folder_name TEXT, status TEXT NOT NULL DEFAULT 'active',
            tags TEXT, meta TEXT, created_at INTEGER NOT NULL, indexed_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            type TEXT NOT NULL, file_count INTEGER NOT NULL, total_size INTEGER NOT NULL,
            used_space INTEGER NOT NULL, total_space INTEGER NOT NULL, detail TEXT, taken_at INTEGER NOT NULL);
        CREATE TABLE sync_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, files_found INTEGER NOT NULL DEFAULT 0,
            files_indexed INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
            error TEXT, start_at INTEGER NOT NULL, end_at INTEGER);
        CREATE TABLE groups (group_id TEXT PRIMARY KEY, group_name TEXT,
            join_time INTEGER, last_sync_at INTEGER, sync_cursor TEXT);
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);
        INSERT INTO groups (group_id, group_name) VALUES ('g1', '旧群');
        INSERT INTO resources (resource_id, group_id, type, name, size, source_ref,
            created_at, indexed_at, updated_at)
            VALUES ('g1:file:old1', 'g1', 'file', 'old.zip', 100, 'old1', 1, 1, 1);
        """
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_migration_v1_to_v2_preserves_data(tmp_path):
    db_path = tmp_path / "old.db"
    await _build_v1_db(db_path)
    s = SqliteMetaStore(db_path)
    await s.init()
    # 新列存在
    import sqlite3

    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(groups)")]
    assert "role" in cols and "display_name" in cols
    assert "sort_order" in cols and "label" in cols and "last_scan_at" in cols
    # 老数据保留
    row = conn.execute("SELECT group_name, role FROM groups WHERE group_id='g1'").fetchone()
    assert row == ("旧群", "unknown")
    page = await s.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert page.total == 1
    assert (
        conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        == _SCHEMA_VERSION
    )
    cols3 = [r[1] for r in conn.execute("PRAGMA table_info(groups)")]
    assert "used_space" in cols3 and "total_space" in cols3 and "file_count" in cols3
    conn.close()
    # 二次 init 幂等（ALTER 不重复执行）
    s2 = SqliteMetaStore(db_path)
    await s2.init()
    await s2.close()


@pytest.mark.asyncio
async def test_upsert_groups_and_reorder(store):
    from core.domain.sync import GroupInfo

    n = await store.upsert_groups(
        [
            GroupInfo(group_id="g1", group_name="研发", role="owned", sort_order=2),
            GroupInfo(group_id="g2", group_name="素材", role="admin"),
        ]
    )
    assert n == 2
    # 重复 upsert 幂等更新
    await store.upsert_groups([GroupInfo(group_id="g1", group_name="研发2", role="owned")])
    groups = await store.list_groups()
    assert {g.group_id for g in groups} == {"g1", "g2"}
    g1 = next(g for g in groups if g.group_id == "g1")
    assert g1.group_name == "研发2" and g1.role == "owned"
    # 排序
    await store.reorder_groups(["g2", "g1"])
    groups = await store.list_groups()
    assert [g.group_id for g in groups] == ["g2", "g1"]
    # 字段更新（display_name/label）
    await store.update_group_fields("g1", display_name="研发协作", label="A")
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.shown_name == "研发协作" and g1.label == "A"


@pytest.mark.asyncio
async def test_update_group_fields_rejects_unknown(store):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        await store.update_group_fields("g1", evil="1; DROP TABLE groups")


@pytest.mark.asyncio
async def test_query_resources_carries_type(store):
    """v9 回归：PageItem 必须携带 type（webapi 类型徽标渲染依赖，曾 500）。"""
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType
    from core.domain.sync import ResourceQuery

    await store.upsert_resources([
        Resource(
            group_id="g1", type=ResourceType.ALBUM, name="旅行", source_ref="a1", size=0, created_at=100,
        ),
        Resource(
            group_id="g1", type=ResourceType.ESSENCE, name="重要通知", source_ref="m1", size=0, created_at=200,
        ),
        Resource(
            group_id="g1", type=ResourceType.FILE, name="a.pdf", source_ref="f1", size=10, created_at=300,
        ),
    ])
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file"))
    assert page.total == 1 and page.items[0].type == "file"
    page = await store.query_resources(ResourceQuery(group_id="g1", type="album"))
    assert page.total == 1 and page.items[0].type == "album"
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert page.total == 1 and page.items[0].type == "essence"


@pytest.mark.asyncio
async def test_album_essence_reconcile(store):
    """v9 一致性：云端已消失的相册/精华条目随采集对账删除。"""
    from core.domain.sync import ResourceQuery

    await store.upsert_album_essence(
        "g1",
        [{"album_id": "a1", "name": "旅行"}],
        [{"message_id": "m1", "content": [{"type": "text", "data": {"text": "通知"}}]}],
    )
    assert (await store.query_resources(
        ResourceQuery(group_id="g1", type="album"))).total == 1
    # 云端相册删除，精华保留 → 相册行对账删除
    await store.upsert_album_essence(
        "g1", [],
        [{"message_id": "m1", "content": [{"type": "text", "data": {"text": "通知"}}]}],
    )
    assert (await store.query_resources(
        ResourceQuery(group_id="g1", type="album"))).total == 0
    assert (await store.query_resources(
        ResourceQuery(group_id="g1", type="essence"))).total == 1
    # 全部清空 → 精华行也对账删除
    await store.upsert_album_essence("g1", [], [])
    assert (await store.query_resources(
        ResourceQuery(group_id="g1", type="essence"))).total == 0


@pytest.mark.asyncio
async def test_reconcile_protects_text_split_essence(store):
    """v1.2：对账删除不清理自建拆分精华行（source_ref 非云端 message_id）。"""
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType
    from core.domain.sync import ResourceQuery

    await store.upsert_resources([
        Resource(
            group_id="g1", type=ResourceType.ESSENCE, name="长文",
            source_ref="text:abc123", size=5000,
            meta={"kind": "text_split", "parts": [{"seq": 1}]},
        ),
        Resource(
            group_id="g1", type=ResourceType.ESSENCE, name="普通精华",
            source_ref="m1",
        ),
    ])
    # 云端对账：两者都不在云端列表（模拟全部消失）→ 仅普通精华被删
    await store.upsert_album_essence("g1", [], [])
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert page.total == 1 and page.items[0].name == "长文"


@pytest.mark.asyncio
async def test_orphan_cleanup_protects_volume_parents(store):
    """v1.2：孤儿清理不删除分卷/视频父资源（source_ref 为本地合成键）。"""
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType

    await store.upsert_resources([
        Resource(
            group_id="g1", type=ResourceType.FILE, name="movie.mp4",
            source_ref="vidgroup:x", meta={"volumes": True, "kind": "video"},
        ),
        Resource(
            group_id="g1", type=ResourceType.FILE, name="old.pdf",
            source_ref="f_old",
        ),
    ])
    await store.mark_missing_as_deleted("g1", True, {"f_keep"})
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file"))
    by_name = {it.name: it for it in page.items}
    assert "movie.mp4" in by_name  # 父资源受保护，保持 active
    assert page.total == 1         # old.pdf 已被软删（active 查询不可见）


@pytest.mark.asyncio
async def test_resource_tags_and_cloud(store):
    """v1.3：标签覆盖写入 + 聚合 + 查询过滤。"""
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType
    from core.domain.sync import ResourceQuery

    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="a.pdf",
                 source_ref="fa", size=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="b.pdf",
                 source_ref="fb", size=1),
    ])
    await store.update_resource_tags(1, ["工作", "重要"])
    await store.update_resource_tags(2, ["工作"])
    cloud = await store.tag_cloud()
    assert cloud == [{"tag": "工作", "count": 2}, {"tag": "重要", "count": 1}]
    page = await store.query_resources(
        ResourceQuery(group_id="g1", tags=["重要"]))
    assert page.total == 1 and page.items[0].name == "a.pdf"
    assert page.items[0].tags == ["工作", "重要"]
    # 覆盖清空
    await store.update_resource_tags(1, [])
    cloud = await store.tag_cloud()
    assert cloud == [{"tag": "工作", "count": 1}]


@pytest.mark.asyncio
async def test_tag_cloud_read_before_any_write(tmp_path):
    """回归（v2.9.1）：硬刷新首请求为标签云读取（无任何先写），不得 AttributeError。"""
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    try:
        cloud = await s.tag_cloud()  # 冷启动直接读
        assert cloud == []
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_list_accounts_aggregation(store):
    """v2.11：账号聚合（统一/单独管理数据源）。"""
    from core.domain.sync import GroupInfo
    await store.upsert_groups([
        GroupInfo(group_id="g1", role="owned", account_id="10001"),
        GroupInfo(group_id="g2", role="owned", account_id="10001"),
        GroupInfo(group_id="g3", role="owned", account_id="20002"),
    ])
    accounts = await store.list_accounts()
    assert accounts == [{"account_id": "10001", "groups": 2},
                        {"account_id": "20002", "groups": 1}]
