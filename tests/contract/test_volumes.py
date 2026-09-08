"""分卷管线与选群策略测试（P3，docs/09 §14.1/§14.2）。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import GroupInfo  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.files.consts import (  # noqa: E402
    CHUNK_THRESHOLD_BYTES,
    VOLUME_SIZE_BYTES,
)
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from core.application.catalog import StoragePlanner  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


@pytest.fixture
async def env(tmp_path, monkeypatch):
    # 缩小分卷参数以便测试（patch 模块常量）
    import core.application.files.consts as fo

    monkeypatch.setattr(fo, "CHUNK_THRESHOLD_BYTES", 8 * 1024)
    monkeypatch.setattr(fo, "VOLUME_SIZE_BYTES", 4 * 1024)

    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_groups([GroupInfo(group_id="g1", account_id="10001")])
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ops: FileOpsService | None = None
    queue = OpQueue(lambda op: ops.handle(op), interval=0.0)
    await queue.start()
    ops = FileOpsService(api, store, queue, sync, tmp_dir=tmp_path / "tmp")
    yield tmp_path, store, api, queue, ops
    await queue.shutdown()
    await store.close()


async def _drain_op(queue, task_id, timeout=8.0):
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        st = await queue.status()
        recent = [r for r in st["recent"] if r["task_id"] == task_id]
        if recent:
            return recent[0]
        await asyncio.sleep(0.05)
    raise TimeoutError("op not finished")


@pytest.mark.asyncio
async def test_volume_upload_cut_and_upload(env, monkeypatch):
    tmp_path, store, api, queue, ops = env
    # 不可压缩数据：必选 zip 后仍 > 8KB → 4KB/卷 → 4 卷
    data = os.urandom(12800)
    src = tmp_path / "big.bin"
    src.write_bytes(data)
    assert src.stat().st_size > 8 * 1024

    tid = await ops.submit_volume_upload("g1", src.as_posix(), "big.bin")
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    # 逐卷上传调用（先切原始分片，再逐片 zip 压缩上传）
    vol_calls = [c for c in api.calls if c.startswith("upload_group_file:g1:big.part")]
    assert len(vol_calls) == 4, vol_calls
    assert all(c.endswith(".zip") for c in vol_calls)
    # volumes 表注册 + uploaded
    from core.domain.sync import VolumeInfo

    # 通过资源查询父 resource_id
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10, keyword="big.bin"
        )
    )
    assert page.total == 1
    parent_id = page.items[0].resource_id  # volumes 键=完整 resource_id
    vols = await store.list_volumes(parent_id)
    assert len(vols) == 4
    assert all(v.status == "uploaded" for v in vols)
    assert all(v.sha256 for v in vols)
    # 命名规范：{stem}.part{seq:02d}of{total:02d}.zip
    assert [v.part_name for v in sorted(vols, key=lambda x: x.seq)] == [
        f"big.part{i:02d}of04.zip" for i in range(1, 5)
    ]
    # 父资源 meta：组合信息 + 原始内容 total_sha256
    detail = await store.get_resource_by_resource_id(page.items[0].resource_id)
    meta = detail["meta"] or {}
    assert meta.get("volumes") is True
    assert meta.get("compression") == "zip-part"
    assert meta.get("original_name") == "big.bin"
    assert meta.get("original_size") == len(data)
    assert meta.get("total_sha256") == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_volume_download_recombine(env, monkeypatch):
    # 捕获逐卷上传的真实字节（必选 zip 后的 zip 分片），下载重组时回放
    captured: dict[str, bytes] = {}

    async def _cap_upload(group, path, name, **kw):
        captured[name] = Path(path).read_bytes()
        return None

    tmp_path, store, api, queue, ops = env
    monkeypatch.setattr(ops.api, "upload_group_file", _cap_upload)
    # 不可压缩数据：zip 后仍 > 8KB 阈值 → 走分卷而非普通上传
    data = os.urandom(12800)
    src = tmp_path / "big.bin"
    src.write_bytes(data)
    await ops.submit_volume_upload("g1", src.as_posix(), "big.bin")
    await asyncio.sleep(0.6)  # 等 op 完成
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10, keyword="big.bin"
        )
    )
    # 回填 source_ref（fake：upload 后 sync 空树 → 需手动模拟回填）
    parent_id = page.items[0].resource_id  # volumes 键=完整 resource_id
    vols = await store.list_volumes(parent_id)
    for v in vols:
        await store.update_volume_fields(parent_id, v.seq,
                                         source_ref=f"volfile_{v.seq}", busid=102)

    by_ref = {f"volfile_{v.seq}": captured[v.part_name] for v in vols}

    async def _fake_fetch(self, url):
        return by_ref[url.rsplit("/", 1)[-1]]

    monkeypatch.setattr(
        "core.application.files.FileOpsService._fetch_bytes", _fake_fetch
    )
    target, name = await ops.download_info("g1", page.items[0].id)
    assert name == "big.bin"
    recombined = Path(target).read_bytes()
    assert recombined == data  # zip 总哈希校验 + 自动解压还原 == 原文
    Path(target).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_backfill_volume_refs(env):
    """同步后按 part 文件名回填卷 source_ref（上传接口不返回 file_id）。"""
    tmp_path, store, api, queue, ops = env
    from core.domain.sync import ResourceQuery

    # 模拟：父资源 + 两卷已上传；同步后 resources 中出现 part 文件（fake 树）
    parent = "g1:file:volgroup:test"
    await store.upsert_resources([Resource(
        group_id="g1", type=ResourceType.FILE, name="big.bin",
        source_ref="volgroup:test", size=9999, created_at=1, meta={"volumes": True},
    )])
    from core.domain.sync import VolumeInfo

    await store.insert_volumes([
        VolumeInfo(parent_resource_id=parent, seq=1, part_name="big.part01of02.zip", status="uploaded"),
        VolumeInfo(parent_resource_id=parent, seq=2, part_name="big.part02of02.zip", status="uploaded"),
    ])
    # 模拟同步后索引：part 文件落库（backfill 从 DB 匹配文件名）
    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="big.part01of02.zip",
                 source_ref="volf1", size=5000, busid=102, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="big.part02of02.zip",
                 source_ref="volf2", size=4999, busid=102, created_at=1),
    ])
    await ops.backfill_volume_refs("g1", parent)
    vols = await store.list_volumes(parent)
    assert vols[0].source_ref == "volf1" and vols[1].source_ref == "volf2"


@pytest.mark.asyncio
async def test_event_driven_volume_backfill(env):
    """group_upload 事件驱动：part 文件事件自动回填未就绪分卷引用。"""
    tmp_path, store, api, queue, ops = env
    from core.domain.sync import VolumeInfo, ResourceQuery
    from core.application.sync import ResourceSyncService

    parent = "g1:file:volgroup:evt"
    await store.insert_volumes([
        VolumeInfo(parent_resource_id=parent, seq=1, part_name="big.part01of02.zip", status="uploaded"),
        VolumeInfo(parent_resource_id=parent, seq=2, part_name="big.part02of02.zip", status="uploaded"),
    ])
    sync = ResourceSyncService(api, store)
    # 模拟群成员手动补传 part01 的事件
    ok = await sync.index_event({
        "post_type": "notice", "notice_type": "group_upload",
        "group_id": "g1", "user_id": "7", "time": 1,
        "file": {"id": "evtfile_1", "name": "big.part01of02.zip", "size": 100, "busid": 102},
    })
    assert ok is True
    vols = await store.list_volumes(parent)
    assert vols[0].source_ref == "evtfile_1" and vols[0].busid == 102  # 回填
    assert vols[1].source_ref is None  # 未匹配，保持待回填
    # 非 part 事件不触发回填
    await sync.index_event({
        "post_type": "notice", "notice_type": "group_upload",
        "group_id": "g1", "user_id": "7", "time": 1,
        "file": {"id": "evt_2", "name": "normal.txt", "size": 1, "busid": 1},
    })
    vols = await store.list_volumes(parent)
    assert vols[0].source_ref == "evtfile_1"


# ---------- StoragePlanner ----------

@pytest.mark.asyncio
async def test_planner_pick_group_by_free_space(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    planner = StoragePlanner(store)
    groups = [
        GroupInfo(group_id="a", role="owned", used_space=9_500_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="b", role="owned", used_space=1_000_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="c", role="member", used_space=8_000_000_000, total_space=10_000_000_000),
    ]
    # 新语义：owned 有序池首个可用（排序优先，非余量最大）
    pick = await planner.pick_group(groups)
    assert pick.group_id == "a"  # a 为 owned 池排序首个且可用
    # 容量过滤：请求 5GB → owned 池仅 b 可用 → b
    pick2 = await planner.pick_group(groups, requested_bytes=5_000_000_000)
    assert pick2.group_id == "b"
    # owned 全不足 → 扩展全池（溢出切换）
    pick3 = await planner.pick_group(
        [GroupInfo(group_id="m", role="member", used_space=0, total_space=10_000_000_000)],
        requested_bytes=8_000_000_000,
    )
    assert pick3.group_id == "m"
    await store.close()


@pytest.mark.asyncio
async def test_planner_pick_min_group_id_n07(tmp_path):
    """2026-09-01 N-07：相册/精华缺省 = 群号值最小（忽略 owned/sort_order 偏好）。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    planner = StoragePlanner(store)
    groups = [
        GroupInfo(group_id="9001", role="member", used_space=0, total_space=10_000_000_000),
        GroupInfo(group_id="0123", role="owned", used_space=9_000_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="0456", role="owned", used_space=1_000_000_000, total_space=10_000_000_000),
    ]
    pick = await planner.pick_min_group_id(groups)
    assert pick.group_id == "0123"  # 群号最小（数字序）
    assert await planner.pick_min_group_id([]) is None
    await store.close()


@pytest.mark.asyncio
async def test_planner_pick_min_group_for_size_n07(tmp_path):
    """2026-09-01 N-07：群文件缺省 = 群号最小且剩余空间 > 待传大小。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    planner = StoragePlanner(store)
    groups = [
        GroupInfo(group_id="0300", role="member", used_space=9_500_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="0100", role="member", used_space=9_800_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="0200", role="owned", used_space=1_000_000_000, total_space=10_000_000_000),
    ]
    # 群号最小 0100 余量 200MB < 500MB → 跳过；0200 余量 9GB 足够 → 0200
    pick = await planner.pick_min_group_for_size(groups, requested_bytes=500_000_000)
    assert pick.group_id == "0200"
    # 全部不足 → 余量最大群（溢出语义）
    pick2 = await planner.pick_min_group_for_size(groups, requested_bytes=9_900_000_000)
    assert pick2.group_id == "0200"
    assert await planner.pick_min_group_for_size([]) is None
    await store.close()


def test_capacity_state_matrix():
    assert StoragePlanner.capacity_state(5_000_000_000, 10_000_000_000) == "ok"
    assert StoragePlanner.capacity_state(9_200_000_000, 10_000_000_000) == "warn"
    assert StoragePlanner.capacity_state(9_900_000_000, 10_000_000_000) == "danger"
    assert StoragePlanner.capacity_state(0, 0) == "unknown"


def test_capacity_stats_aggregate():
    groups = [
        GroupInfo(group_id="a", role="owned", used_space=9_500_000_000, total_space=10_000_000_000),
        GroupInfo(group_id="b", role="owned", used_space=1_000_000_000, total_space=10_000_000_000),
    ]
    st = StoragePlanner.capacity_stats(groups)
    assert st["groups"] == 2
    assert st["total_space"] == 20_000_000_000
    assert st["used_space"] == 10_500_000_000
    assert len(st["alerts"]) == 1 and st["alerts"][0]["group_id"] == "a"

# ---------- v7 目录持久化 + SearchKV ----------

@pytest.mark.asyncio
async def test_folders_persist_and_searchkv(tmp_path):
    from adapters.persistence.sqlite import SqliteMetaStore
    from core.application.catalog import SearchKV
    from core.domain.sync import ResourceQuery

    store = SqliteMetaStore(tmp_path / "f.db")
    await store.init()
    kv = SearchKV(store)
    # 目录持久化
    await store.upsert_folders("g1", [
        {"folder_id": "d1", "folder_name": "资料", "parent_id": ""},
        {"folder_id": "d2", "folder_name": "图片", "parent_id": "d1"},
    ])
    det = await store.list_folders_detail("g1")
    assert {d["folder_name"] for d in det} == {"资料", "图片"}
    # 文件落库后 KV 懒构建 + 前缀搜索
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType

    await store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="设计稿.pdf",
                 source_ref="f1", size=1, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="设计素材.zip",
                 source_ref="f2", size=1, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="报告.docx",
                 source_ref="f3", size=1, created_at=1),
    ])
    await kv.ensure_group("g1")
    ids = await kv.match_ids("g1", "设计")
    assert len(ids) == 2  # 设计稿 + 设计素材
    ids2 = await kv.match_ids("g1", "报告")
    assert len(ids2) == 1
    # 事件维护（v2.9：FTS 触发器自动同步，删除后查询即排除）
    await store.update_resource_fields(ids[0], status="deleted")
    kv.mark_dirty("g1")
    await kv.ensure_group("g1")
    leftover = await kv.match_ids("g1", "设计")
    assert len(leftover) == 1
    await store.close()


@pytest.mark.asyncio
async def test_volume_zip_compress_reassembles(env, monkeypatch):
    """C-4（2026-09-03）：zip 压缩分卷 → 重组后自动解压还原（可逆）。

    手工构造「zip 压缩分卷」资源（meta=volumes+compression=zip+total_sha256），
    卷数据=确定性 zip 字节（writestr 固定 mtime）——验证解压分支：
    逐卷校验 → 汇总校验 → 解压 → 原名返回。
    """
    import io as _io
    import zipfile as _zf

    tmp_path, store, api, queue, ops = env
    data = bytes(range(96)) * 50
    bio = _io.BytesIO()
    info = _zf.ZipInfo("doc.bin", date_time=(2020, 1, 1, 0, 0, 0))
    with _zf.ZipFile(bio, "w") as zf:
        zf.writestr(info, data)
    zip_data = bio.getvalue()
    import hashlib as _hl

    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name="doc.bin",
                source_ref="zip-parent",
                size=len(zip_data),
                created_at=int(time.time()),
                meta={
                    "volumes": True,
                    "compression": "zip",
                    "original_name": "doc.bin",
                    "total_sha256": _hl.sha256(zip_data).hexdigest(),
                },
            )
        ]
    )
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10, keyword="doc.bin"
        )
    )
    rid = page.items[0].resource_id
    mid = 1 + len(zip_data) // 2
    from core.domain.sync import VolumeInfo

    for seq, chunk in ((1, zip_data[:mid]), (2, zip_data[mid:])):
        await store.insert_volumes(
            [
                VolumeInfo(
                    parent_resource_id=rid,
                    seq=seq,
                    part_name=f"doc.zip.part{seq:02d}",
                    size=len(chunk),
                    sha256=_hl.sha256(chunk).hexdigest(),
                    status="uploaded",
                    source_ref=f"v{seq}",
                    busid=1,
                )
            ]
        )

    async def _fake_fetch(self, url):
        mid2 = 1 + len(zip_data) // 2
        n = int(url.rsplit("v", 1)[-1])
        return zip_data[:mid2] if n == 1 else zip_data[mid2:]

    monkeypatch.setattr(
        "core.application.files.FileOpsService._fetch_bytes", _fake_fetch
    )
    target, name = await ops.download_info("g1", page.items[0].id)
    assert name == "doc.bin"
    assert Path(target).read_bytes() == data  # 解压还原 == 原文
    Path(target).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_volume_upload_compress_sets_meta(env, monkeypatch):
    """分卷压缩为内置必选：submit_volume_upload 先切后压 → meta.compression=zip-part。"""
    tmp_path, store, api, queue, ops = env
    src = tmp_path / "big.bin"
    src.write_bytes(b"z" * 9000)
    await ops.submit_volume_upload("g1", src.as_posix(), "big.bin")
    await asyncio.sleep(0.6)
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10, keyword="big.bin"
        )
    )
    detail = await store.get_resource_detail("g1", page.items[0].id)
    meta = detail.get("meta") or {}
    assert meta.get("compression") == "zip-part"
    assert meta.get("original_name") == "big.bin"
    assert meta.get("original_size") == 9000


@pytest.mark.asyncio
async def test_convert_volumes_payload_always_zip(env, monkeypatch):
    """转分卷内置化：payload 不再有 compress 开关，恒携带原文件名。"""
    tmp_path, store, api, queue, ops = env
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 12000)
    payloads = []
    orig = ops.queue.submit

    async def _capture(kind, target="", payload=None, account=None):
        payloads.append(payload or {})
        return "t0"

    monkeypatch.setattr(ops.queue, "submit", _capture)
    from core.domain.sync import ResourceQuery

    # 直测 submit_convert_volumes 的 payload（detail 需存在）
    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name="big.bin",
                source_ref="ref1", size=12000, uploader_id="10001",
            )
        ]
    )
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=5))
    await ops.submit_convert_volumes("g1", page.items[0].id)
    assert payloads and "compress" not in payloads[-1]
    assert payloads[-1].get("original_name") == "big.bin"


@pytest.mark.asyncio
async def test_sweep_convert_volumes_submits_over_threshold(env, monkeypatch):
    """内置自动转分卷（sweep）：只提交超阈值、非组合、无待处理任务的文件，
    且受 per-sweep limit 限流（防部署后洪峰）。"""
    tmp_path, store, api, queue, ops = env

    # 挂起 convert_volumes 执行，使任务停留在 pending（验证 has_pending 去重）
    release = asyncio.Event()

    async def _hang_convert(op):
        await release.wait()

    monkeypatch.setattr(ops, "_do_convert_volumes", _hang_convert)

    from core.domain.sync import ResourceQuery

    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name="big.bin",
                source_ref="f1", size=20 * 1024, busid=102, created_at=1,
                uploader_id="10001",
            ),
            Resource(
                group_id="g1", type=ResourceType.FILE, name="small.bin",
                source_ref="f2", size=100, busid=103, created_at=2,
            ),
            Resource(
                group_id="g1", type=ResourceType.FILE, name="vol.7z",
                source_ref="f3", size=20 * 1024, busid=104, created_at=3,
                meta={"volumes": True},
            ),
        ]
    )
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=5))
    big_id = next(i.id for i in page.items if i.name == "big.bin")

    n = await ops.sweep_convert_volumes("g1")
    assert n == 1  # 仅 big.bin（small 低于阈值；vol.7z 已是组合形态）
    for _ in range(50):  # 等待队列 worker 领取任务
        st = await queue.status()
        if any(r["kind"] == "convert_volumes" for r in st.get("running", [])):
            break
        await asyncio.sleep(0.02)
    st = await queue.status()
    assert any(r["kind"] == "convert_volumes" for r in st.get("running", []))

    # 待处理任务存在 → 重复 sweep 去重，不重复提交
    n2 = await ops.sweep_convert_volumes("g1")
    assert n2 == 0

    release.set()
    await asyncio.sleep(0.3)

    # per-sweep limit：两个新大文件只提交 limit 个
    await store.upsert_resources(
        [
            Resource(
                group_id="g1", type=ResourceType.FILE, name=f"bulk{i}.bin",
                source_ref=f"fb{i}", size=20 * 1024, busid=200 + i,
                created_at=10 + i, uploader_id="10001",
            )
            for i in (1, 2)
        ]
    )
    n3 = await ops.sweep_convert_volumes("g1", limit=1)
    assert n3 == 1
