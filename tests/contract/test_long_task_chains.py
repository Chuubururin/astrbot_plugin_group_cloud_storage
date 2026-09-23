"""未测长任务链补测：队列 → OpDispatcher → 服务 → 适配器/存储 全链。

盘点结论（2026-09-13）：此前仅 sync/file_scan/convert_volumes 有队列级
覆盖；fetch、upload、delete、essence_save、video_upload、netdisk_index、
batch_groups 等长任务只测过服务层或提交层，队列级工作链（任务控制 +
SSE 事件 + 容量/索引回写）从未端到端跑过。本文件逐条补齐。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.openlist import NetFile  # noqa: E402
from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from commands.handlers import Services  # noqa: E402
from core.application.catalog import ResourceQueryService, StatsService  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.netdisk import NetdiskService  # noqa: E402
from core.application.queue import OpDispatcher, OpQueue  # noqa: E402
from core.application.sync import GroupScanService, ResourceSyncService  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.sync import GroupInfo  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402
from tests.fixtures.fake_openlist import FakeOpenListClient  # noqa: E402


async def _drain(queue: OpQueue, n: int = 1, timeout: float = 12.0) -> dict:
    """等待 n 个 op 全部终态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        if len(st["recent"]) >= n and not st["running"] and st["depth"] == 0:
            return st
        await asyncio.sleep(0.05)
    raise TimeoutError(f"queue drain timeout: {await queue.status()}")


async def _state_of(queue: OpQueue, task_id: str) -> dict | None:
    return next(
        (r for r in (await queue.status())["recent"] if r["task_id"] == task_id),
        None,
    )


async def _wait_state(queue: OpQueue, task_id: str, state: str,
                      timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = await _state_of(queue, task_id)
        if rec is not None and rec["state"] == state:
            return rec
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"task {task_id} never reached {state}: {await _state_of(queue, task_id)}"
    )


class _Events:
    """订阅队列事件流；测试结束时 cancel 收集任务。"""

    def __init__(self, queue: OpQueue):
        self.queue = queue
        self.items: list[dict] = []
        self._task = asyncio.create_task(self._listen())

    async def _listen(self):
        async for ev in self.queue.subscribe():
            self.items.append(ev)

    def stop(self):
        self._task.cancel()

    def of(self, type_: str, kind: str | None = None) -> list[dict]:
        return [
            e for e in self.items
            if e.get("type") == type_ and (kind is None or e.get("kind") == kind)
        ]


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    cell: dict = {}
    queue = OpQueue(lambda op: cell["d"].handle(op),
                    backoff_base=0.05)
    sync = ResourceSyncService(api, store)
    scan = GroupScanService(api, store, queue)
    tmp = tmp_path / "tmp"
    ops = FileOpsService(api, store, queue, sync, tmp_dir=tmp,
                         config={"managed_groups": []})
    ingest = CloudIngestService(
        api, store, queue, sync, tmp_dir=tmp,
        config={"essence_chunk_size": 1000, "video_segment_seconds": 600,
                "fetch_max_bytes": 10 * 1024 * 1024, "fetch_timeout_sec": 10},
    )
    services = Services(
        permission=None, store=store, api=api, sync=sync,
        query=ResourceQueryService(store), stats=StatsService(store),
        scan=scan, ops=ops, ingest=ingest, queue=queue,
        config={"managed_groups": []},
    )
    dispatcher = OpDispatcher(
        services, api, store, sync, scan, ingest, None, ops, queue,
        services.config, bots_getter=lambda: [],
    )
    cell["d"] = dispatcher
    await queue.start()
    ns = SimpleNamespace(
        store=store, api=api, queue=queue, dispatcher=dispatcher,
        sync=sync, scan=scan, ops=ops, ingest=ingest, tmp=tmp,
        services=services,
    )
    yield ns
    await queue.shutdown()
    await store.close()


# ---------- fetch：云端拉取长任务全链 ----------


@pytest.mark.asyncio
async def test_fetch_to_group_file_full_chain(env, monkeypatch):
    """fetch → 下载 → 上传群文件 → 全量同步 → data_changed。"""

    async def _fake_download(self, url: str, dest: Path) -> int:
        dest.write_bytes(b"hello-fetch")
        return 11

    monkeypatch.setattr(CloudIngestService, "_download", _fake_download)
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "fetch", target="g1",
            payload={"url": "http://cdn/x/hello.bin", "name": "hello.txt"},
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert "upload_group_file:g1:hello.txt" in env.api.calls
    assert ev.of("data_changed", "fetch"), "fetch 完成必须广播 data_changed"


@pytest.mark.asyncio
async def test_fetch_cancel_lands_at_post_download_checkpoint(env, monkeypatch):
    """运行中取消：下载无法中断，但下载后的 pause_check 必须拦截，
    不得把已取消任务的文件上传到群。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_download(self, url: str, dest: Path) -> int:
        started.set()
        await release.wait()
        dest.write_bytes(b"x")
        return 1

    monkeypatch.setattr(CloudIngestService, "_download", _slow_download)
    tid = await env.queue.submit(
        "fetch", target="g1",
        payload={"url": "http://cdn/x/f.bin", "name": "f.bin"},
    )
    await asyncio.wait_for(started.wait(), 5.0)
    assert env.queue.cancel_task(tid) is True
    release.set()
    rec = await _wait_state(env.queue, tid, "cancelled")
    assert "upload_group_file" not in " ".join(env.api.calls)


# ---------- upload/delete：文件操作链（容量回写 + 删除后全量重同步） ----------


@pytest.mark.asyncio
async def test_upload_chain_capacity_and_announce(env):
    f = env.tmp / "up.txt"
    f.write_bytes(b"upload-payload")
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "upload", target="g1", payload={"path": str(f), "name": "up.txt"}
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert "upload_group_file:g1:up.txt" in env.api.calls
    # run_file_op_and_announce 尾部链：searchkv 为 None 跳过，但容量回写
    # （get_group_file_system_info）与 data_changed 必须发生
    assert "get_group_file_system_info" in env.api.calls
    assert ev.of("data_changed", "upload")


@pytest.mark.asyncio
async def test_delete_chain_runs_full_resync_after_cloud_delete(env):
    """delete 全链：云端删除 → 行标 deleted → 全量重同步 → 容量 → 广播。"""
    env.api.tree = {
        None: (
            [
                {"file_id": "file_1", "name": "a.txt", "size": 10, "busid": 102,
                 "uploader_id": "10001", "uploader_name": "Alice",
                 "upload_time": 1700000001},
                {"file_id": "file_2", "name": "b.txt", "size": 20, "busid": 102,
                 "uploader_id": "10001", "uploader_name": "Alice",
                 "upload_time": 1700000002},
            ],
            [],
        ),
    }
    tid1 = await env.queue.submit("sync", target="g1")
    await _drain(env.queue, n=1)
    details = [
        await env.store.get_resource_detail("g1", rid)
        for rid in (1, 2)
    ]
    by_name = {d["name"]: d for d in details if d}
    assert set(by_name) == {"a.txt", "b.txt"}
    root_calls_before = sum(
        1 for c in env.api.calls if c == "get_group_root_files"
    )

    ev = _Events(env.queue)
    try:
        tid2 = await env.queue.submit(
            "delete", target="g1",
            payload={"id": by_name["a.txt"]["id"]},
        )
        await _drain(env.queue, n=2)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid2)
    assert rec["state"] in ("ok", "done"), rec
    assert "delete_group_file:g1:file_1" in env.api.calls
    # 删除后强制全量重同步（strict view 语义）：root 列表调用增加
    root_calls_after = sum(
        1 for c in env.api.calls if c == "get_group_root_files"
    )
    assert root_calls_after > root_calls_before
    assert ev.of("data_changed", "delete")


# ---------- essence_save：精华拆分存储链 ----------


@pytest.mark.asyncio
async def test_essence_save_chain_sends_and_sets(env):
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "essence_save", target="g1",
            payload={"title": "标题", "text": "精华正文"},
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert env.api.sent_messages, "精华保存必须先发群消息"
    assert env.api.essence_set, "发消息后必须设为精华"
    assert ev.of("data_changed", "essence_save")


# ---------- video_upload：短视频直传链 ----------


@pytest.mark.asyncio
async def test_video_upload_short_direct_chain(env, monkeypatch):
    """可探测时长 < 上限 → 直接上传路径（分段转码路径由 video_album 覆盖）。"""

    async def _probe(self, path):
        return 10.0

    monkeypatch.setattr(CloudIngestService, "_probe_duration", _probe)
    f = env.tmp / "clip.mp4"
    f.write_bytes(b"0" * 1024)
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "video_upload", target="g1",
            payload={"path": str(f), "name": "clip.mp4"},
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert "upload_group_file:g1:clip.mp4" in env.api.calls
    assert ev.of("data_changed", "video_upload")


# ---------- netdisk_index：网盘深度索引链（进度含 task_id + 目录间可取消） ----------


def _netdisk_tree(client: FakeOpenListClient) -> None:
    client.files["/"] = [
        NetFile(name="sub1", size=0, is_dir=True, modified=0),
        NetFile(name="sub2", size=0, is_dir=True, modified=0),
        NetFile(name="a.txt", size=5, is_dir=False, modified=0),
    ]
    client.files["/sub1/"] = [
        NetFile(name="b.txt", size=6, is_dir=False, modified=0),
    ]
    client.files["/sub2/"] = [
        NetFile(name="c.txt", size=7, is_dir=False, modified=0),
    ]


@pytest.mark.asyncio
async def test_netdisk_index_chain_writes_rows_and_progress(env):
    nd = NetdiskService(FakeOpenListClient(), env.store,
                        {"type_ext_overrides": {}}, env.queue)
    _netdisk_tree(nd._client)
    env.services.netdisk = nd
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "netdisk_index", target="g1", payload={"path": "/"}
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    rows = await env.store.get_netdisk_meta("/")
    paths = {r["remote_path"] for r in rows}
    assert {"/a.txt", "/sub1/", "/sub1/b.txt", "/sub2/c.txt"} <= paths
    # 进度事件必须携带 task_id（前端任务 Tab 关联依据）
    progress = ev.of("progress", "netdisk_index")
    assert progress and all(p.get("task_id") == tid for p in progress)


@pytest.mark.asyncio
async def test_netdisk_index_cancel_between_directories(env, monkeypatch):
    """深度索引在目录粒度可取消：卡在 sub1 的列表上取消，恢复后下一个
    目录的 pause_check 必须终止（sub2 及之后不入库）。
    注意取消必须落在非最后目录：协作式取消只在 checkpoint 生效。"""
    client = FakeOpenListClient()
    _netdisk_tree(client)
    gate = asyncio.Event()
    release = asyncio.Event()
    orig = client.list_dir_page

    async def gated(path, page, per_page=200):
        if path == "/sub1/":
            gate.set()
            await release.wait()
        return await orig(path, page, per_page)

    monkeypatch.setattr(client, "list_dir_page", gated)
    nd = NetdiskService(client, env.store, {"type_ext_overrides": {}},
                        env.queue)
    env.services.netdisk = nd
    tid = await env.queue.submit(
        "netdisk_index", target="g1", payload={"path": "/"}
    )
    await asyncio.wait_for(gate.wait(), 5.0)
    assert env.queue.cancel_task(tid) is True
    release.set()
    await _wait_state(env.queue, tid, "cancelled")
    rows = {r["remote_path"] for r in await env.store.get_netdisk_meta("/")}
    assert "/sub1/" in rows        # 已索引目录保留
    assert "/sub2/c.txt" not in rows  # 取消后的目录不再入库


# ---------- batch_groups：多群批量操作链 ----------


@pytest.mark.asyncio
async def test_batch_groups_rename_chain(env):
    await env.store.upsert_groups([
        GroupInfo(group_id="g1", group_name="旧A"),
        GroupInfo(group_id="g2", group_name="旧B"),
    ])
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "batch_groups", target="*",
            payload={"action": "rename", "value": "新名",
                     "group_ids": ["g1", "g2"]},
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert "set_group_name:g1:新名" in env.api.calls
    assert "set_group_name:g2:新名" in env.api.calls
    groups = {g.group_id: g.group_name for g in await env.store.list_groups()}
    assert groups["g1"] == "新名" and groups["g2"] == "新名"
    progress = ev.of("progress", "batch_groups")
    assert [(p["i"], p["n"]) for p in progress] == [(1, 2), (2, 2)]


# ---------- 分发守卫：未知 kind 快速失败且不重试 ----------


@pytest.mark.asyncio
async def test_unknown_kind_fails_without_retry(env):
    tid = await env.queue.submit("totally_unknown", target="g1", payload={})
    rec = await _wait_state(env.queue, tid, "failed")
    assert "unknown op kind" in (rec["error"] or "")


# ---------- file_scan mode=all：多群全量扫描链 ----------


@pytest.mark.asyncio
async def test_file_scan_all_mode_chain(env):
    """mode=all 走 list_page_groups（非白名单=全部群），逐群全量同步落库。"""
    env.api.tree = {
        None: (
            [
                {"file_id": "file_1", "name": "a.txt", "size": 10, "busid": 102,
                 "uploader_id": "10001", "uploader_name": "Alice",
                 "upload_time": 1700000001},
            ],
            [],
        ),
    }
    await env.store.upsert_groups([
        GroupInfo(group_id="g1", group_name="群一"),
        GroupInfo(group_id="g2", group_name="群二"),
    ])
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "file_scan", target="*", payload={"mode": "all"}
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    # 两群各自全量同步并落库（全局自增 id：g1→1，g2→2）
    d1 = await env.store.get_resource_detail("g1", 1)
    d2 = await env.store.get_resource_detail("g2", 2)
    assert d1 and d1["name"] == "a.txt"
    assert d2 and d2["name"] == "a.txt"
    progress = ev.of("progress", "file_scan")
    assert progress and all(p["n"] == 2 for p in progress)
    # 扫描结束的全局刷新事件（带 ts、无 i/n，与逐群事件区分）
    tail = [
        e for e in ev.of("data_changed", "file_scan")
        if e.get("target") == "*" and "ts" in e
    ]
    assert tail, "扫描结束必须有全局 data_changed"


# ---------- diff_file_scan：枯萎差分链 ----------


@pytest.mark.asyncio
async def test_diff_file_scan_withers_absent_rows(env):
    """差分同步：云端缺失的行软删（枯萎），云端存在的行保留；
    云端列表不完整时冻结不修剪（fail-closed，另见 resource_sync 测试）。"""
    env.api.tree = {
        None: (
            [
                {"file_id": "file_1", "name": "a.txt", "size": 10, "busid": 102,
                 "uploader_id": "10001", "uploader_name": "Alice",
                 "upload_time": 1700000001},
            ],
            [],
        ),
    }
    tid1 = await env.queue.submit("sync", target="g1")
    await _drain(env.queue, n=1)
    assert await env.store.get_resource_any(1) is not None
    # 种一个云端已不存在的陈旧行
    await env.store.upsert_resources([
        Resource(group_id="g1", type=ResourceType.FILE, name="gone.txt",
                 source_ref="gone_file", size=1, created_at=1),
    ])
    assert await env.store.get_resource_any(2) is not None
    ev = _Events(env.queue)
    try:
        tid2 = await env.queue.submit("diff_file_scan", target="g1", payload={})
        await _drain(env.queue, n=2)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid2)
    assert rec["state"] in ("ok", "done"), rec
    # get_resource_any 仅返回 active 行：陈旧行被枯萎，存活行保留
    assert await env.store.get_resource_any(2) is None
    assert await env.store.get_resource_any(1) is not None
    assert ev.of("data_changed", "diff_file_scan")


# ---------- essence_delete：精华删除链 ----------


@pytest.mark.asyncio
async def test_essence_delete_chain_removes_parts_and_soft_deletes(env):
    """删除精华：逐分片撤精华 → 行软删（云端为准，撤失败容忍 cloud-miss）。"""
    tid1 = await env.queue.submit(
        "essence_save", target="g1",
        payload={"title": "标题", "text": "精华正文"},
    )
    await _drain(env.queue, n=1)
    row = await env.store.get_resource_any(1)
    assert row and row["type"] == "essence"
    parts = (row.get("meta") or {}).get("parts") or []
    assert parts, "保存的精华必须带分片元数据"
    ev = _Events(env.queue)
    try:
        tid2 = await env.queue.submit(
            "essence_delete", target="g1",
            payload={"id": row["id"], "parts": parts},
        )
        await _drain(env.queue, n=2)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid2)
    assert rec["state"] in ("ok", "done"), rec
    assert env.api.essence_deleted == [str(p["message_id"]) for p in parts]
    assert await env.store.get_resource_any(row["id"]) is None
    assert ev.of("data_changed", "essence_delete")


# ---------- create_folder：建目录链 ----------


@pytest.mark.asyncio
async def test_create_folder_chain(env):
    """建目录：协议端创建 + 全量同步刷新（当前实现不经 announce 广播）。"""
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "create_folder", target="g1", payload={"name": "新目录"}
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert any(
        c.startswith("create_group_file_folder:g1:新目录") for c in env.api.calls
    )


# ---------- image_album：相册导入链 ----------


@pytest.mark.asyncio
async def test_image_album_chain(env):
    """相册导入：缺相册时自动创建 → 暂存文件重命名为声明名 → 上传。"""
    f = env.tmp / "staged_x.png"
    f.write_bytes(b"\x89PNG fake")
    ev = _Events(env.queue)
    try:
        tid = await env.queue.submit(
            "image_album", target="g1",
            payload={"path": str(f), "name": "照片.png", "album_name": "相册A"},
        )
        await _drain(env.queue)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid)
    assert rec["state"] in ("ok", "done"), rec
    assert "create_group_album:g1:相册A" in env.api.calls
    ups = [u for u in env.api.album_uploads if u["album_name"] == "相册A"]
    assert ups and ups[0]["group_id"] == "g1"
    assert ups[0]["file"].endswith("照片.png")
    assert ev.of("data_changed", "image_album")


# ---------- replace_name：改名重传链 ----------


@pytest.mark.asyncio
async def test_replace_name_chain_reupload_and_resync(env, monkeypatch):
    """改名重传：下载原文件 → 新名重传 → 删旧 → 索引替换 → 重同步回填新
    source_ref（fake 的上传/删除真实作用于云端树，验证全闭环）。"""

    async def _fake_download(self, url: str, dest: Path) -> int:
        dest.write_bytes(b"renamed-bytes")
        return 13

    monkeypatch.setattr(FileOpsService, "_download_to_file", _fake_download)

    orig_up = env.api.upload_group_file

    async def _up(group_id, file_path, name="", folder_id=None, folder="",
                  upload_file=True):
        await orig_up(group_id, file_path, name, folder_id=folder_id,
                      folder=folder, upload_file=upload_file)
        files, folders = env.api.tree[None]
        files.append({
            "file_id": f"file_{len(files) + 1}", "name": name, "size": 13,
            "busid": 102, "uploader_id": "10001", "uploader_name": "Alice",
            "upload_time": 1700000009,
        })

    orig_del = env.api.delete_group_file

    async def _del(group_id, file_id, busid=None):
        await orig_del(group_id, file_id, busid)
        files, folders = env.api.tree[None]
        env.api.tree[None] = (
            [f for f in files if f["file_id"] != file_id], folders,
        )

    monkeypatch.setattr(env.api, "upload_group_file", _up)
    monkeypatch.setattr(env.api, "delete_group_file", _del)

    env.api.tree = {
        None: (
            [
                {"file_id": "file_1", "name": "old.txt", "size": 10,
                 "busid": 102, "uploader_id": "10001", "uploader_name": "Alice",
                 "upload_time": 1700000001},
            ],
            [],
        ),
    }
    tid1 = await env.queue.submit("sync", target="g1")
    await _drain(env.queue, n=1)
    detail = await env.store.get_resource_detail("g1", 1)
    assert detail and detail["name"] == "old.txt"

    ev = _Events(env.queue)
    try:
        tid2 = await env.queue.submit(
            "replace_name", target="g1",
            payload={
                "id": detail["id"], "file_id": detail["source_ref"],
                "busid": detail.get("busid") or 0, "name": "old.txt",
                "new_name": "new.txt", "folder": detail.get("folder_id") or "",
            },
        )
        await _drain(env.queue, n=2)
    finally:
        ev.stop()
    rec = await _state_of(env.queue, tid2)
    assert rec["state"] in ("ok", "done"), rec
    assert "upload_group_file:g1:new.txt" in env.api.calls
    assert "delete_group_file:g1:file_1" in env.api.calls
    # 同一行就地更新：logical_key 随名字改写，重同步把新 file_id 回填回来。
    # 不再 delete+insert 换 id —— 改名后旧名的回归会命中同一 logical_key，
    # 若留旧 key 会把改名行覆盖掉（见 test_logical_key_convergence 的改名用例）。
    row = await env.store.get_resource_any(1)
    assert row and row["name"] == "new.txt"
    assert row["source_ref"] == "file_2"
    assert await env.store.get_resource_any(2) is None
    assert ev.of("data_changed", "replace_name")
