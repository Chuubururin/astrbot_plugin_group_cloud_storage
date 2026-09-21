"""资源行 upsert 回归测试：软删复活 / 旧格式分卷身份继承 / cloud:// URI 校验。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


def _res(name="f1.zip", source_ref="ref_1", group="g1", size=100,
         status=ResourceStatus.ACTIVE, meta=None):
    return Resource(
        group_id=group, type=ResourceType.FILE, name=name, source_ref=source_ref,
        size=size, uploader_id="10001", uploader_name="Alice", busid=102,
        folder_id="dir1", folder_name="Docs", created_at=1700000001,
        status=status, meta=meta if meta is not None else {},
    )


async def _status(store, resource_id="g1:file:ref_1"):
    row = await store.get_resource_by_resource_id(resource_id)
    return None if row is None else row["status"]


@pytest.mark.asyncio
async def test_deleted_row_is_revived_by_reappearing_active_row(store):
    """M1：不完整列表触发的软删不可逆 —— 同一 file_id 重新出现在云端必须复活。

    upsert 的 status CASE 原来只在「新行非 active」时写入，新行 active 时永远
    保留旧状态，而全库没有任何把 status 置回 active 的路径：一旦
    mark_missing_as_deleted 被不完整列表触发，资源就永久消失。
    """
    await store.upsert_resources([_res()])
    assert await store.mark_missing_as_deleted("g1", True, set()) == 1
    assert await _status(store) == "deleted"

    # 同一 file_id 重新出现在云端列表
    await store.upsert_resources([_res()])
    assert await _status(store) == "active"
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert [i.name for i in page.items] == ["f1.zip"]


@pytest.mark.asyncio
async def test_active_upsert_does_not_clobber_archived(store):
    """原有语义保留：active 新行不覆盖 archived（只复活 deleted）。"""
    await store.upsert_resources([_res(status=ResourceStatus.ARCHIVED)])
    await store.upsert_resources([_res()])
    assert await _status(store) == "archived"


@pytest.mark.asyncio
async def test_non_active_upsert_still_wins(store):
    """反向方向：新行 deleted 必须覆盖 active。"""
    await store.upsert_resources([_res()])
    await store.upsert_resources([_res(status=ResourceStatus.DELETED)])
    assert await _status(store) == "deleted"


@pytest.mark.asyncio
async def test_legacy_volumes_parent_identity_is_inherited(store):
    """旧格式分卷父行（meta={'volumes': True, 'kind': 'video'}，无 composition
    键，见 core/application/ingest/video.py）也必须参与身份继承，否则会话切换后
    新 file_id 的行拿不到分卷元数据、volumes 重挂不生效。"""
    parent_ref = "vidgroup:abc123"
    await store.upsert_resources([
        _res(name="big.mp4", source_ref=parent_ref, size=1000,
             meta={"volumes": True, "kind": "video", "total_sha256": "deadbeef"}),
    ])
    # 新会话：同一 (group, name) 以新 file_id 出现，旧 source_ref 不在列表里
    await store.upsert_resources([_res(name="big.mp4", source_ref="fresh_id", size=1000)])

    assert await store.get_resource_by_resource_id(f"g1:file:{parent_ref}") is None
    successor = await store.get_resource_by_resource_id("g1:file:fresh_id")
    assert successor is not None
    meta = successor["meta"] or {}
    assert meta.get("volumes") is True
    assert meta.get("total_sha256") == "deadbeef"


@pytest.mark.asyncio
async def test_volume_part_queries_declare_escape_on_every_like(store):
    """F-1c：分卷查询里每个 LIKE ? 都必须声明 ESCAPE。

    两条语句原先只在 part_name 子句写了 ESCAPE，parent_resource_id 子句没有。
    虽然 group_id 是纯数字群号（commands.py 校验 ≥5 位）使通配符无法注入，
    但一致性缺失会让下一位读者以为"这条没风险"，且一旦 scoping 前缀改带用户
    可控段就立刻可利用。本断言钉住 SQL 文本本身。
    """
    import inspect

    from adapters.persistence.sqlite.volumes import VolumesMixin

    src = inspect.getsource(VolumesMixin.backfill_volume_by_part)
    assert src.count("LIKE ?") == 1, "backfill 的 LIKE 子句数变了，请同步本测试"
    assert "parent_resource_id LIKE ? ESCAPE" in src, (
        "backfill_volume_by_part 的 parent_resource_id LIKE 缺 ESCAPE"
    )
    src2 = inspect.getsource(VolumesMixin.has_volume_part)
    assert src2.count("LIKE ?") == 2, "has_volume_part 的 LIKE 子句数变了，请同步本测试"
    # 两个 LIKE 都必须各自带 ESCAPE（原先只有 part_name 那个有）。
    assert re.findall(r"LIKE \?[^E]*?ESCAPE", src2).__len__() == 2, (
        "has_volume_part 存在未声明 ESCAPE 的 LIKE"
    )


@pytest.mark.asyncio
async def test_volume_part_glob_matches_only_literal_stem(store):
    """F-1c 行为侧：生产 glob 必须命中字面 stem，且被通配的字符不得越界。

    命名规则来自 core/application/files/volume.py：
        part_name = f"{stem}.part{seq:02d}of{total:02d}.zip"
    即 ``<stem>.partNNofMM.zip``（各两位）。查询 glob 为
    ``<escaped stem>%.part%of__.zip``，其中尾部 ``__`` 恰好吃掉 ``MM``。
    这里同时钉住两件事：字面下划线能命中、把一个字符改成别的东西就命中不了。
    """
    from core.domain.sync import VolumeInfo

    await store.insert_volumes([
        VolumeInfo(parent_resource_id="g1:file:parent", seq=1,
                   part_name="a_b.part01of02.zip", source_ref="r1", busid=1),
    ])

    # 字面 stem（下划线已转义）必须命中
    assert await store.has_volume_part("g1", r"a\_b%.part%of__.zip") is True
    # 同一位置换成别的字符，不得命中 —— 证明不是靠通配蒙中的
    assert await store.has_volume_part("g1", r"a\_Xb%.part%of__.zip") is False
    # 完全不同的 stem 不得命中
    assert await store.has_volume_part("g1", r"zzz%.part%of__.zip") is False


@pytest.mark.asyncio
async def test_get_by_uri_validates_group_and_type(store):
    """URI 的 group_id/type 两段必须参与校验：否则 cloud://<任意群>/<任意类型>/<id>
    都能取到该 id 的资源（webapi/resources_mutation.py 直接对外暴露该入口）。"""
    await store.upsert_resources([_res()])
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id

    ok = await store.get_by_uri(f"cloud://g1/file/{rid}")
    assert ok is not None and ok["id"] == rid

    assert await store.get_by_uri(f"cloud://g2/file/{rid}") is None
    assert await store.get_by_uri(f"cloud://g1/album/{rid}") is None

    with pytest.raises(ValueError):
        await store.get_by_uri(f"http://g1/file/{rid}")
    with pytest.raises(ValueError):
        await store.get_by_uri("cloud://g1/file/not-a-number")
