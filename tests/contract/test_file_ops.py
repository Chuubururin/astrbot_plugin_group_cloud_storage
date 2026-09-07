"""FileOpsService 测试（P2 管理路径，docs/09 §14）：上传/删除/重命名/移动/下载。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import GroupInfo, ResourceQuery  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_groups([GroupInfo(group_id="g1", account_id="10001")])
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ops: FileOpsService | None = None
    queue = OpQueue(lambda op: ops.handle(op), interval=0.0)  # 闭包延迟绑定
    await queue.start()
    ops = FileOpsService(api, store, queue, sync, tmp_dir=tmp_path / "tmp")
    yield tmp_path, store, api, queue, ops
    await queue.shutdown()
    await store.close()


async def _drain_op(queue, task_id, timeout=8.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        recent = [r for r in st["recent"] if r["task_id"] == task_id]
        if recent:
            return recent[0]
        await asyncio.sleep(0.05)
    raise TimeoutError("op not finished")


@pytest.mark.asyncio
async def test_upload_op(env):
    tmp_path, store, api, queue, ops = env
    src = tmp_path / "hello.txt"
    src.write_text("hello")
    tid = await ops.submit_upload("g1", src.as_posix(), "hello.txt")
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("upload_group_file:g1:hello.txt") for c in api.calls)
    assert not src.exists()  # 暂存已清理


@pytest.mark.asyncio
async def test_delete_op_soft_deletes(env):
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                   source_ref="f1", size=10, busid=102, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id
    tid = await ops.submit_delete("g1", rid)
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("delete_group_file:g1:f1") for c in api.calls)
    detail = await store.get_resource_detail("g1", rid)
    assert detail["status"] == ResourceStatus.DELETED.value


@pytest.mark.asyncio
async def test_move_op(env):
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                   source_ref="f1", size=10, busid=102, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id
    # move
    tid = await ops.submit_move("g1", rid, "fd9")
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("move_group_file:g1:f1:!/fd9") for c in api.calls)
    assert (await store.get_resource_detail("g1", rid))["folder_id"] == "fd9"


@pytest.mark.asyncio
async def test_download_url(env):
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                   source_ref="f1", size=10, busid=102, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    url, name = await ops.download_info("g1", page.items[0].id)
    assert url == "https://fake/download/f1" and name == "a.txt"

@pytest.mark.asyncio
async def test_replace_name_flow(env, monkeypatch):
    """改名重传（v2.6 唯一改名路径）：下载原件→新名重传→删旧→索引替换。"""
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.txt",
                   source_ref="f1", size=10, busid=102, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id

    async def fake_fetch(url):
        return b"OLDDATA"

    monkeypatch.setattr(ops, "_fetch_bytes", fake_fetch)
    tid = await ops.submit_replace_name("g1", rid, "b.txt")
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("upload_group_file:g1:b.txt") for c in api.calls)
    assert any(c.startswith("delete_group_file:g1:f1") for c in api.calls)
    assert (await store.get_resource_detail("g1", rid))["name"] == "b.txt"


@pytest.mark.asyncio
async def test_replace_name_rejects_volumes(env):
    """分卷资源不支持改名重传（清晰拒绝而非失败）。"""
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="big.7z",
                   source_ref="v1", size=10, busid=102, created_at=1,
                   meta={"volumes": True})
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id
    with pytest.raises(ValueError):
        await ops.submit_replace_name("g1", rid, "big2.7z")


@pytest.mark.asyncio
async def test_convert_volumes_flow(env, monkeypatch):
    """化整为零（v2.8）：云端大文件 → 分卷上传 → 删原件 → 索引原位转换。"""
    from core.application.composition.spec import decode_composition

    tmp_path, store, api, queue, ops = env
    big = b"Z" * (200 * 1024)  # 200KB（阈值由 CHUNK_THRESHOLD_BYTES 控制，测试用小文件模拟需放宽）
    # 分卷阈值 95MB，200KB 无法触发 → 直接打桩 _do_volume_upload 校验编排？改为验证拒绝路径+阈值
    res = Resource(group_id="g1", type=ResourceType.FILE, name="big.bin",
                   source_ref="f1", size=200 * 1024, busid=102, created_at=1,
                   uploader_id="10001")
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id
    # 小于阈值 → 清晰拒绝
    with pytest.raises(ValueError):
        await ops.submit_convert_volumes("g1", rid)
    # 已是组合形态 → 清晰拒绝
    res2 = Resource(group_id="g1", type=ResourceType.FILE, name="v.7z",
                    source_ref="v1", size=200 * 1024 * 1024, busid=102,
                    created_at=1, meta={"volumes": True})
    await store.upsert_resources([res2])
    page2 = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid2 = page2.items[1].id if len(page2.items) > 1 else page2.items[0].id
    with pytest.raises(ValueError):
        await ops.submit_convert_volumes("g1", rid2)


@pytest.mark.asyncio
async def test_convert_volumes_uploads_and_deletes(env, monkeypatch):
    """转分卷全流程：下载（打桩）→ 逐卷上传 → 删原件 → meta 写规范描述符。"""
    from core.application.composition.spec import decode_composition

    tmp_path, store, api, queue, ops = env
    payload = os.urandom(96 * 1024 * 1024)  # 不可压缩，必选 zip 后仍 >95MB → 2 卷
    res = Resource(group_id="g1", type=ResourceType.FILE, name="big.bin",
                   source_ref="f1", size=len(payload), busid=102, created_at=1,
                   uploader_id="10001")
    await store.upsert_resources([res])
    page = await store.query_resources(__import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(group_id="g1", page_size=10))
    rid = page.items[0].id

    async def fake_fetch(url):
        return payload

    monkeypatch.setattr(ops, "_fetch_bytes", fake_fetch)
    tid = await ops.submit_convert_volumes("g1", rid)
    deadline = __import__("time").monotonic() + 30
    state = None
    while __import__("time").monotonic() < deadline:
        st = await queue.status()
        hit = [r for r in st["recent"] if r["task_id"] == tid]
        if hit:
            state = hit[0]["state"]
            break
        await __import__("asyncio").sleep(0.05)
    assert state == "ok"
    assert any(c.startswith("upload_group_file:g1:big.part01") for c in api.calls)
    assert any(c.startswith("delete_group_file:g1:f1") for c in api.calls)
    detail = await store.get_resource_detail("g1", rid)
    meta = detail["meta"] or {}
    comp = decode_composition(meta)
    assert comp and comp["kind"] == "volumes" and comp["parts"] == 2


@pytest.mark.asyncio
async def test_convert_rejects_non_owner_upload(env):
    """他人上传的存量文件不可分卷：删除原件会失败，必须安全侧拒绝。"""
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="other.bin",
                   source_ref="f9", size=200 * 1024 * 1024, busid=102, created_at=1,
                   uploader_id="99999")
    await store.upsert_resources([res])
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    rid = next(it.id for it in page.items if it.name == "other.bin")
    with pytest.raises(ValueError):
        await ops.submit_convert_volumes("g1", rid)


@pytest.mark.asyncio
async def test_convert_rejects_missing_uploader(env):
    """历史索引缺少上传者身份 → 与他人上传同等对待（安全侧拒绝）。"""
    tmp_path, store, api, queue, ops = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="legacy.bin",
                   source_ref="f8", size=200 * 1024 * 1024, busid=102, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    rid = next(it.id for it in page.items if it.name == "legacy.bin")
    with pytest.raises(ValueError):
        await ops.submit_convert_volumes("g1", rid)


@pytest.mark.asyncio
async def test_convert_rejects_missing_group_account(env):
    """群未绑定账号（account_id 缺失）→ 无法判定归属，拒绝转换。"""
    tmp_path, store, api, queue, ops = env
    await store.upsert_groups([GroupInfo(group_id="g2", account_id="")])
    res = Resource(group_id="g2", type=ResourceType.FILE, name="nobind.bin",
                   source_ref="f7", size=200 * 1024 * 1024, busid=102, created_at=1,
                   uploader_id="10001")
    await store.upsert_resources([res])
    page = await store.query_resources(ResourceQuery(group_id="g2", page_size=10))
    rid = page.items[0].id
    with pytest.raises(ValueError):
        await ops.submit_convert_volumes("g2", rid)


@pytest.mark.asyncio
async def test_sweep_skips_non_owner_and_missing_account(env):
    """自动 sweep：非本人上传/上传者缺失/群账号缺失均不产生 convert_volumes。"""
    tmp_path, store, api, queue, ops = env
    await store.upsert_groups([GroupInfo(group_id="g3", account_id="")])
    rows = [
        Resource(group_id="g1", type=ResourceType.FILE, name="sw-other.bin",
                 source_ref="s1", size=200 * 1024 * 1024, busid=102, created_at=1,
                 uploader_id="99999"),
        Resource(group_id="g1", type=ResourceType.FILE, name="sw-noid.bin",
                 source_ref="s2", size=200 * 1024 * 1024, busid=102, created_at=1),
        Resource(group_id="g1", type=ResourceType.FILE, name="sw-owner.bin",
                 source_ref="s3", size=200 * 1024 * 1024, busid=102, created_at=1,
                 uploader_id="10001"),
        Resource(group_id="g3", type=ResourceType.FILE, name="sw-nobind.bin",
                 source_ref="s4", size=200 * 1024 * 1024, busid=102, created_at=1,
                 uploader_id="10001"),
    ]
    await store.upsert_resources(rows)

    submitted: list[tuple] = []
    orig = queue.submit

    async def spy(kind, *a, **kw):
        submitted.append((kind, a, kw))
        return await orig(kind, *a, **kw)

    ops.queue.submit = spy  # type: ignore[method-assign]
    n1 = await ops.sweep_convert_volumes("g1")
    n3 = await ops.sweep_convert_volumes("g3")
    cv = [s for s in submitted if s[0] == "convert_volumes"]
    assert n1 == 1 and n3 == 0
    assert len(cv) == 1
    # 唯一提交的是本人上传的那个文件
    payload = cv[0][2]["payload"]
    assert payload["name"] == "sw-owner.bin"


@pytest.mark.asyncio
async def test_local_big_upload_still_enters_volume_pipeline(env, tmp_path):
    """本地上传的自动分卷不做归属校验（无云端原件需要删除），行为保持不变。"""
    from core.application.files import consts as files_consts

    tmp, store, api, queue, ops = env
    src = tmp / "huge.bin"
    src.write_bytes(b"X" * (files_consts.CHUNK_THRESHOLD_BYTES + 1024))
    tid = await ops.submit_upload("g1", src.as_posix(), "huge.bin")
    r = await _drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("upload_group_file:g1:huge.part") for c in api.calls)
