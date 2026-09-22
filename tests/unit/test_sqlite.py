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
async def test_same_name_is_the_same_logical_file_regardless_of_size(store):
    """线上 10879 案例，按 logical_key 语义重新钉定（行为已反转，故保留此用例）。

    旧规则：尺寸门用文件大小猜「同名的新文件是不是旧组合文件的接续」，
    2KB 的新文件被判为不同文件 → 不继承 composition、旧行保留、分卷挂旧行。

    新规则：身份是 (group_id, type, name)，同名同群即同一逻辑文件；
    大小只是同步产物（同一云端文件在不同次列表里大小会变），不是身份。
    因此继承 composition 并把分卷重挂到存活行，是正确行为。

    保留用例是为了让后来者看到这次反转是有意为之，而不是误改。
    """
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
    # 同名同群、小得多、自身无 meta 的新文件
    small = Resource(
        group_id="g1", type=ResourceType.FILE, name="vol_test.bin",
        source_ref="new_ref", size=2048, uploader_id="10001", busid=9,
        created_at=1700000002,
    )
    await store.upsert_resources([small])

    page = await store.query_resources(
        ResourceQuery(group_id="g1", keyword="vol_test.bin", page_size=50)
    )
    assert page.total == 1, "一个逻辑文件只应有一行存活"

    d = await store.get_resource_by_resource_id("g1:file:new_ref")
    assert (d["meta"] or {}).get("composition", {}).get("kind") == "volumes"
    vols = await store.list_volumes("g1:file:new_ref")
    assert len(vols) == 1, "分卷必须跟随存活行，不能成为孤儿"
    assert await store.list_volumes("g1:file:old_ref") == []


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
async def test_upsert_resurrects_deleted_row(store):
    """M1：软删不是终态。

    原实现（status=CASE WHEN excluded.status != 'active' THEN excluded.status
    ELSE status END）在新行为 active 时永远保留旧状态，而全库没有把 status
    置回 active 的路径：mark_missing_as_deleted 被一次不完整的列表触发后，
    同一 file_id 即使重新出现在云端也永远回不来。现在两个方向都成立：
    新行 deleted → 置 deleted；新行 active 且旧行 deleted → 复活。
    """
    await store.upsert_resources([_res(1)])
    # 孤儿清理将其置 deleted
    await store.mark_missing_as_deleted("g1", True, {"other"})
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 0
    # 同一文件重新出现在云端列表：复活
    await store.upsert_resources([_res(1)])
    active = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert active.total == 1  # 复活
    assert active.items[0].name == "f1.zip"


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


@pytest.mark.asyncio
async def test_query_resources_exts_filter_matches_transient_intermediates(store):
    """类型过滤（exts）对临时中间态命名保持超集匹配：classify() 剥离临时后缀
    后归入某类型的文件（如 SnowLuma 的 x.rar.netdisk.p.downloading → archive）
    必须能被该类型的 exts 查询命中，与列表显示的分类一致。"""
    from core.domain.resource import Resource

    def _named(name: str, i: int) -> Resource:
        return Resource(
            group_id="g1", type=ResourceType.FILE, name=name, source_ref=f"ref_{i}",
            size=10, uploader_id="10001", uploader_name="Alice", busid=1,
            folder_id="dir1", folder_name="Docs", created_at=1700000000 + i,
        )

    await store.upsert_resources([
        _named("movie.rar", 1),
        _named("movie.rar.netdisk.p.downloading", 2),
        _named("movie.rar.part2", 3),
        _named("notes.txt", 4),
        _named("music.zip.bak", 5),
    ])
    page = await store.query_resources(
        ResourceQuery(group_id="g1", type="file", exts=[".rar"], page_size=100)
    )
    names = {r.name for r in page.items}
    # ".rar" 精确后缀 + ".rar." 中间态变体都命中；".txt"/".bak" 结尾不误入
    assert names == {"movie.rar", "movie.rar.netdisk.p.downloading", "movie.rar.part2"}


@pytest.mark.asyncio
async def test_mark_missing_preserves_volume_parent_after_json_patch(store):
    """回归：分卷父行在 meta 被 json_patch 压缩（紧凑 JSON，无空格）后，
    NOT LIKE '"volumes": true' 字节守卫曾失效，导致父行在普通同步里被误软删。
    现在守卫走 json_extract(meta,'$.volumes')，压缩后仍能识别并保留父行。"""
    from core.domain.resource import Resource
    from core.domain.sync import ResourceQuery

    parent = Resource(
        group_id="g1", type=ResourceType.FILE, name="pkg.rar", size=10,
        source_ref="volgroup:pkg", uploader_id="10001", uploader_name="Alice",
        busid=1, folder_id="dir1", folder_name="Docs", created_at=1700000000,
        meta={"volumes": True, "composition": {"kind": "volumes"}, "note": None},
    )
    # 两次 upsert：第二次走 json_patch 分支，把父行 meta 压成紧凑格式
    await store.upsert_resources([parent])
    await store.upsert_resources([
        Resource(
            group_id="g1", type=ResourceType.FILE, name="pkg.rar", size=10,
            source_ref="volgroup:pkg", uploader_id="10001", uploader_name="Alice",
            busid=1, folder_id="dir1", folder_name="Docs", created_at=1700000000,
            meta={"volumes": True, "composition": {"kind": "volumes"}},
        ),
        Resource(
            group_id="g1", type=ResourceType.FILE, name="plain.txt", size=10,
            source_ref="ref_plain", uploader_id="10001", uploader_name="Alice",
            busid=1, folder_id="dir1", folder_name="Docs", created_at=1700000001,
        ),
    ])
    removed = await store.mark_missing_as_deleted(
        "g1", complete=True, source_file_ids={"whatever"}  # 父行 source_ref 不在云端
    )
    assert removed == 1  # 只删 plain.txt；分卷父行必须被保留
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file", page_size=100))
    names = {r.name for r in page.items}
    assert "pkg.rar" in names
    assert "plain.txt" not in names


@pytest.mark.asyncio
async def test_query_resources_empty_groups_is_empty_set_not_no_filter(store):
    """H8 回归：groups=[] 是空集语义（“匹配任何群”都不成立）→ 必须返回空，
    绝不等于“不过滤”。旧实现 `if q.groups:` 把空列表当假，于是不加任何群过滤；
    webapi 凋零口径在全部账号离线时 target_groups=[]，本应返回空却把全量列表
    泄漏了出去。groups=None 才是“不过滤”。"""
    await store.upsert_resources([_res(1, "g1"), _res(2, "g2")])

    empty = await store.query_resources(ResourceQuery(groups=[], page_size=100))
    assert empty.total == 0
    assert empty.items == []
    # None = 不过滤（跨群全量聚合仍可用）
    everything = await store.query_resources(ResourceQuery(groups=None, page_size=100))
    assert {it.group_id for it in everything.items} == {"g1", "g2"}
    # 非空集合仍走 IN 过滤
    one = await store.query_resources(ResourceQuery(groups=["g2"], page_size=100))
    assert {it.group_id for it in one.items} == {"g2"}


def _corrupt_meta(name: str, value: str = "{oops"):
    """把一个已有行的 meta 写成非法 JSON（L1 场景的库形态）。

    前提：FTS 投影缺失的库——v17 的触发器不在，搜索退化为 LIKE。带触发器的库
    里 UPDATE/INSERT meta 会先在触发器里撞上同一个 json_extract 错误，非法 JSON
    根本写不进去；而“旧文本守卫容错”的历史数据正是这种无触发器的库写下的。
    """

    def _do(conn):
        for trg in ("resources_fts_ai", "resources_fts_ad", "resources_fts_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
        conn.execute("UPDATE resources SET meta=? WHERE name=?", (value, name))
        conn.commit()

    return _do


@pytest.mark.asyncio
async def test_reconcile_survives_malformed_meta_json(store):
    """L1 回归：meta 非 NULL 但非法 JSON 时 json_extract 抛 “malformed JSON”。
    相册/精华每次入库都跑这条 DELETE，一次坏行就中断整个 _reconcile
    （conn.commit() 与随后的 upsert_resources 都不执行）。坏行无法证明自己是
    自建拆分精华行，仍按旧文本守卫的容错口径被对账删除。"""
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.ESSENCE, name="坏行",
                 source_ref="m_bad", size=0),
        Resource(group_id="g1", type=ResourceType.ESSENCE, name="长文",
                 source_ref="text:abc", size=1, meta={"kind": "text_split"}),
    ])

    await store._conn.exec(_corrupt_meta("坏行"))

    def _read_meta(conn):
        return conn.execute("SELECT meta FROM resources WHERE name='坏行'").fetchone()[0]

    assert await store._conn.exec(_read_meta) == "{oops"  # 前提：坏行真的写进去了
    await store.upsert_album_essence("g1", [], [])  # 修复前：malformed JSON 异常
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert {it.name for it in page.items} == {"长文"}


@pytest.mark.asyncio
async def test_upsert_survives_malformed_meta_in_composition_scan(store):
    """L1 回归：_inherit_composition_identity 用 json_extract 扫描 composition
    行，库里一条非法 JSON 的 meta 就让整批 upsert 抛 malformed JSON。加
    json_valid 前置过滤后，坏行只是不被当作 composition 行。"""
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="big.bin",
                 source_ref="old_ref", size=1000, created_at=1,
                 meta={"volumes": True, "composition": {"kind": "volumes"}}),
    ])

    await store._conn.exec(_corrupt_meta("big.bin"))

    def _read_meta(conn):
        return conn.execute("SELECT meta FROM resources WHERE name='big.bin'").fetchone()[0]

    assert await store._conn.exec(_read_meta) == "{oops"  # 前提：坏行真的写进去了
    n = await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="big.bin",
                 source_ref="new_ref", size=1000, created_at=2),
    ])
    assert n >= 1


@pytest.mark.asyncio
async def test_mark_missing_survives_malformed_meta_json(store):
    """L1 回归：清扫的 volumes 守卫走 json_extract，一条非法 JSON 的 meta 让
    整轮清扫抛 malformed JSON（一行都标不了删除）。加 json_valid 后坏行无法
    证明自己是分卷父行，正常参与清扫。"""
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="bad.bin",
                 source_ref="bad_ref", size=10, created_at=1),
    ])

    await store._conn.exec(_corrupt_meta("bad.bin"))

    def _read_meta(conn):
        return conn.execute("SELECT meta FROM resources WHERE name='bad.bin'").fetchone()[0]

    assert await store._conn.exec(_read_meta) == "{oops"  # 前提：坏行真的写进去了
    removed = await store.mark_missing_as_deleted(
        "g1", complete=True, source_file_ids={"other_ref"}
    )
    assert removed == 1


@pytest.mark.asyncio
async def test_keyword_query_survives_malformed_meta_json(store):
    """L1 回归：关键词搜索的 summary 投影走 json_extract(meta, '$.summary')，
    一条非法 JSON 的 meta 就让整次搜索抛 malformed JSON（用户可见的 500）。
    加 json_valid 后坏行只是不参与 summary 匹配。"""
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="good.txt",
                 source_ref="ref_good", size=1, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="bad.txt",
                 source_ref="ref_bad", size=1, created_at=2),
    ])
    await store._conn.exec(_corrupt_meta("bad.txt"))

    page = await store.query_resources(
        ResourceQuery(group_id="g1", keyword="good", page_size=100)
    )
    assert {it.name for it in page.items} == {"good.txt"}


@pytest.mark.asyncio
async def test_reupsert_survives_malformed_meta_json(store):
    """L1 回归：upsert 的 ON CONFLICT 分支用 json_extract(resources.meta, …)
    判断是否 json_patch，库里一条非法 JSON 的 meta 让整批 upsert 抛 malformed
    JSON（坏行永远修不回来）。加 json_valid 后坏行按“无 composition”走 ELSE，
    被本次同步的 meta 覆盖。"""
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="bad.bin",
                 source_ref="ref_bad", size=10, created_at=1),
    ])
    await store._conn.exec(_corrupt_meta("bad.bin"))

    n = await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="bad.bin",
                 source_ref="ref_bad", size=10, created_at=1,
                 meta={"healed": True}),
    ])
    assert n >= 1
    detail = await store.get_resource_by_resource_id("g1:file:ref_bad")
    assert (detail["meta"] or {}).get("healed") is True


@pytest.mark.asyncio
async def test_fts_repair_failure_does_not_veto_version_chain(tmp_path, monkeypatch):
    """M3 回归：事后 FTS 修复块是 best-effort。旧实现 on_skip 恒为 None →
    修复语句失败直接 raise，且与版本链共用同一个 BEGIN IMMEDIATE 事务 →
    所有版本步骤都成功了但版本号不推进；下次启动重试同一处失败 →
    store.init() 每次抛错，插件再也初始化不了（搜索本可退化为 LIKE）。"""
    import sqlite3

    from adapters.persistence.sqlite import migrations as M

    db = tmp_path / "fts_repair.db"
    conn = sqlite3.connect(db)
    conn.execute("BEGIN IMMEDIATE")
    M.migrate(conn)
    conn.commit()
    # 构造“版本标记已推进、扩展 FTS 投影只建了一半”的库
    conn.execute("DROP TABLE resources_fts")
    conn.execute("CREATE VIRTUAL TABLE resources_fts USING fts5(name)")
    conn.commit()
    # 让修复块必然失败
    monkeypatch.setitem(M.MIGRATIONS, 17, ["SELECT * FROM no_such_table_fts_repair"])

    conn.execute("BEGIN IMMEDIATE")
    version = M.migrate(conn)  # 修复前：OperationalError 逃出，整条链被否决
    conn.commit()

    assert version == M.SCHEMA_VERSION
    assert (
        conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        == M.SCHEMA_VERSION
    )
    # 修复块失败只回滚自己：事务仍可用，后续写入照常
    conn.execute("CREATE TABLE _post_repair_probe (x INTEGER)")
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_mark_account_groups_managed_returns_rowcount(store):
    """回归：mark_account_groups_managed 曾漏 return 恒返回 None，
    导致离线检测的 `if n:` 永不命中、_hidden_accounts 不登记、恢复逻辑失效。"""
    from core.domain.sync import GroupInfo

    await store.upsert_groups([
        GroupInfo(group_id="g1", account_id="a1", managed=1),
        GroupInfo(group_id="g2", account_id="a1", managed=1),
        GroupInfo(group_id="g3", account_id="a2", managed=1),
    ])

    n_off = await store.mark_account_groups_managed("a1", 0)
    assert n_off == 2, f"下线置 0 应返回受影响行数，实际 {n_off!r}"

    # 再置 0 已无变化 → 0（幂等）
    assert await store.mark_account_groups_managed("a1", 0) == 0

    n_back = await store.restore_account_groups("a1")
    assert n_back == 2, f"restore_account_groups 应透传行数，实际 {n_back!r}"

    assert await store.mark_account_groups_managed("", 0) == 0


@pytest.mark.asyncio
async def test_mark_account_groups_managed_skips_user_removed(store):
    """managed=1 恢复不得复活用户主动移除（removed=1）的群，行数须如实。"""
    from core.domain.sync import GroupInfo

    await store.upsert_groups([
        GroupInfo(group_id="g1", account_id="a1", managed=1),
        GroupInfo(group_id="g2", account_id="a1", managed=1),
    ])
    await store.mark_account_groups_managed("a1", 0)
    await store.mark_groups_removed(["g2"], 1)

    n = await store.mark_account_groups_managed("a1", 1)
    assert n == 1, "仅 removed=0 的 g1 被恢复"
    groups = {g.group_id: g for g in await store.list_groups(include_hidden=True)}
    assert groups["g1"].managed == 1
    assert groups["g2"].managed == 0, "用户移除的群不被自愈复活"
