"""BridgeService 契约测试（V1.2.1）：记录单任务查询 / 反向记录联动 / 僵尸 pending_in 收敛 / 去重幂等 / D1 改名。

对齐 external_api_design.md v2.4 §5.3 与 15-甲方开发硬限制 附录（D1-D3 P0 前置修复）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.openlist import NetFile, OfflineTask  # noqa: E402
from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import BridgeTaskState, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from core.application.bridge import BridgeService  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402
from tests.fixtures.fake_openlist import FakeDownloadServer, FakeOpenListClient  # noqa: E402
from tests.contract.helpers import drain_op  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fake_download(self, url: str, dest: Path) -> int:
    data = b"BRIDGE-FETCH-" + url.encode() * 32
    dest.write_bytes(data)
    return len(data)


async def _wait_row(store, group_id, rid, direction, want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await store.get_archive_map(group_id, rid, direction)
        if row and row.get("state") == want:
            return row
        await asyncio.sleep(0.05)
    raise TimeoutError(f"archive_map row not {want}")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    client = FakeOpenListClient()
    dlserver = FakeDownloadServer()

    routing: dict = {}

    async def _route(op):
        if op.kind == "bridge_out":
            await routing["bridge"].handle_bridge_out(op)
        elif op.kind == "bridge_in":
            await routing["bridge"].handle_bridge_in(op)
        elif op.kind == "sync":
            # 镜像生产 op_dispatch 的 sync 分支（bridge_in 直传成功后会
            # 入队组级 sync 收敛索引）
            await sync.run_full_sync(
                op.target,
                routing.setdefault("_sync_locks", {}).setdefault(
                    op.target, asyncio.Lock()
                ),
            )
        else:
            await routing["ingest"].handle(op)

    queue = OpQueue(_route, interval=0.0)
    await queue.start()
    ingest = CloudIngestService(
        api,
        store,
        queue,
        sync,
        tmp_dir=tmp_path / "tmp",
        config={"fetch_max_bytes": 10 * 1024 * 1024, "fetch_timeout_sec": 10},
    )
    # 拉取下载字节走假实现（不真实联网）
    monkeypatch.setattr(CloudIngestService, "_download", _fake_download)
    config = SimpleNamespace(
        openlist_poll_interval_sec=0.0,
        openlist_dst_dir="/",
        openlist_dst_dir_template="{group_id}/{filename}",
        bridge_min_bytes=0,
        bridge_max_bytes=0,
    )
    bridge = BridgeService(client, store, config, queue, api, ingest, dlserver)
    routing["bridge"] = bridge
    routing["ingest"] = ingest

    # 资源：g1/video.mp4（1024B）
    await store.upsert_resources(
        [
            Resource(
                group_id="g1",
                type=ResourceType.FILE,
                name="video.mp4",
                source_ref="fs://g1/video.mp4",
                size=1024,
                created_at=int(time.time()),
            )
        ]
    )
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file"))
    rid = page.items[0].id

    yield SimpleNamespace(
        store=store,
        api=api,
        queue=queue,
        client=client,
        bridge=bridge,
        ingest=ingest,
        dlserver=dlserver,
        rid=rid,
    )
    await bridge.stop_polling()
    await queue.shutdown()
    await store.close()


# ---------- 记录单任务查询（status(task_id)） ----------


@pytest.mark.asyncio
async def test_status_single_task_lookup(env):
    ns = env
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    st = await ns.bridge.status(ol_tid)
    assert st["state"] == BridgeTaskState.PENDING.value
    assert st["direction"] == "out"
    assert st["group_id"] == "g1"
    assert st["remote_path"] == "/g1/video.mp4"
    missing = await ns.bridge.status("no-such-task")
    assert missing == {
        "task_id": "no-such-task",
        "state": BridgeTaskState.UNKNOWN.value,
    }


# ---------- D1：UUID 文件名 → 完成后控制面改名 ----------


@pytest.mark.asyncio
async def test_bridge_out_poll_done_renames_uuid(env):
    ns = env
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    uuid_name = "3f2b1a9c8d7e4f5a6b8c9d0e1f2a3b4c"
    # 任务完成 + 远端出现 UUID 命名文件（OpenList 离线下载行为）
    ns.client.undone.clear()
    ns.client.done_tasks[ol_tid] = OfflineTask(
        id=ol_tid,
        name="video.mp4",
        state="succeeded",
        status="done",
        progress=100.0,
        error="",
    )
    ns.client.files["/g1"] = [
        NetFile(name=uuid_name, size=1024, is_dir=False, modified=""),
    ]
    ns.bridge._interval = 0.05
    ns.bridge._ensure_poll_task()
    row = await _wait_row(ns.store, "g1", ns.rid, "out", "done")
    # D1：按大小匹配 UUID 文件并改名，remote_path 回写记录
    assert ns.client.renames == [(f"/g1/{uuid_name}", "video.mp4")]
    assert row["remote_path"] == "/g1/video.mp4"
    # 群回执走消息链（send_group_msg 契约）
    assert any("video.mp4" in m["text"] for m in ns.api.sent_messages)


# ---------- D3：重复提交去重（幂等 + 远端探活） ----------


@pytest.mark.asyncio
async def test_dedup_skips_when_done_and_remote_present(env):
    ns = env
    await ns.store.upsert_archive_map(
        {
            "resource_id": ns.rid,
            "group_id": "g1",
            "task_id": "t0",
            "remote_path": "/g1/video.mp4",
            "direction": "out",
            "state": BridgeTaskState.DONE.value,
            "updated_at": _now(),
        }
    )
    ns.client.files["/g1"] = [
        NetFile(name="video.mp4", size=1024, is_dir=False, modified=""),
    ]
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    assert ns.client.submitted == []  # 已归档且远端存在 → 跳过重提


@pytest.mark.asyncio
async def test_resubmit_after_remote_deleted(env):
    ns = env
    await ns.store.upsert_archive_map(
        {
            "resource_id": ns.rid,
            "group_id": "g1",
            "task_id": "t0",
            "remote_path": "/g1/video.mp4",
            "direction": "out",
            "state": BridgeTaskState.DONE.value,
            "updated_at": _now(),
        }
    )
    # 远端文件已被删除 → 记录清理后重提
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    assert len(ns.client.submitted) == 1
    row = await ns.store.get_archive_map("g1", ns.rid, "out")
    assert row["state"] == BridgeTaskState.PENDING.value
    assert row["task_id"] == next(iter(ns.client.undone))


# ---------- REQ-16：下载服务守卫 ----------


@pytest.mark.asyncio
async def test_out_guard_dlserver_disabled(env):
    ns = env
    guarded = BridgeService(
        ns.client,
        ns.store,
        SimpleNamespace(
            openlist_poll_interval_sec=0.0,
            openlist_dst_dir="/",
            openlist_dst_dir_template="{group_id}/{filename}",
            bridge_min_bytes=0,
            bridge_max_bytes=0,
        ),
        ns.queue,
        ns.api,
        ns.ingest,
        FakeDownloadServer(enabled=False),
    )
    op = SimpleNamespace(
        kind="bridge_out", target="g1", payload={"resource_id": ns.rid}
    )
    await guarded.handle_bridge_out(op)
    assert ns.client.submitted == []


# ---------- 僵尸 pending_in 记录收敛（B 类必要自动，REQ-18） ----------


@pytest.mark.asyncio
async def test_zombie_pending_in_converged_on_recover(env):
    ns = env
    await ns.store.upsert_archive_map(
        {
            "resource_id": 0,
            "group_id": "g1",
            "task_id": "fetch_old",
            "remote_path": "/netdisk/old.bin",
            "direction": "in",
            "state": BridgeTaskState.PENDING.value,
            "updated_at": _now(),
        }
    )
    await ns.bridge.recover()
    row = await ns.store.get_archive_map_by_task("fetch_old")
    assert row["state"] == BridgeTaskState.FAILED.value


# ---------- REQ-14：URL 直传失败自动降级 fetch + 记录联动 ----------


@pytest.mark.asyncio
async def test_in_url_upload_degrades_to_fetch(env, monkeypatch):
    ns = env

    async def _boom(self, group_id, file_path, name, folder_id=None):
        self.calls.append(f"upload_group_file:{group_id}:{name}")
        if str(file_path).startswith("http"):
            raise RuntimeError("URL upload unsupported")
        # 本地路径（fetch 管线）正常成功

    monkeypatch.setattr(FakeOneBotApi, "upload_group_file", _boom)

    tid = await ns.bridge.submit_in("netdisk/movie.mp4", group_id="g1")
    await drain_op(ns.queue, tid)
    # 能力缓存被运行期失败降级
    assert ns.bridge._url_upload_capable is False
    assert ns.api.calls.count("upload_group_file:g1:movie.mp4") == 2
    # 记录行由 ledger 任务联动 fetch 完成事件 → done
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = await ns.store.list_archive_map(
            states=("pending", "done", "failed"), direction="in"
        )
        if row and row[0]["state"] == BridgeTaskState.DONE.value:
            break
        await asyncio.sleep(0.05)
    else:
        raise TimeoutError("pending_in row never converged to done")
    assert ns.bridge._ledger_task is None or ns.bridge._ledger_task.done()


@pytest.mark.asyncio
async def test_in_url_upload_queues_index_sync(env):
    """直传成功后文件不经本地管线 → 需要自动入队一次组级 sync 让索引收敛；
    去重生效（同组已有 sync 时不再叠加）。"""
    ns = env
    tid = await ns.bridge.submit_in("netdisk/movie.mp4", group_id="g1")
    await drain_op(ns.queue, tid)
    row = await ns.store.list_archive_map(
        states=("done",), direction="in"
    )
    assert row and row[0]["state"] == BridgeTaskState.DONE.value
    # 收敛 sync 已入队（可能已执行完）：从 ledger 最近记录里确认 sync 出现过
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = await ns.queue.status()
        sync_ops = [r for r in status["recent"] if r["kind"] == "sync"]
        if sync_ops:
            assert sync_ops[0]["target"] == "g1"
            break
        await asyncio.sleep(0.05)
    else:
        raise TimeoutError("post-upload sync never queued")


# ---------- 手动模式读修复（Bug：poll=0 时单任务/聚合查询永停 pending） ----------


@pytest.mark.asyncio
async def test_status_read_repairs_pending_row_in_manual_mode(env):
    """interval=0（手动模式）下，status(task_id) 读到 pending 行时对账 OpenList。"""
    ns = env
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    # OpenList 侧任务已完成，但本地无轮询，ledger 仍 pending
    ns.client.undone.clear()
    ns.client.done_tasks[ol_tid] = OfflineTask(
        id=ol_tid,
        name="video.mp4",
        state="succeeded",
        status="done",
        progress=100.0,
        error="",
    )
    ns.client.files["/g1"] = [
        NetFile(name="video.mp4", size=1024, is_dir=False, modified=""),
    ]
    st = await ns.bridge.status(ol_tid)
    assert st["state"] == BridgeTaskState.DONE.value
    row = await ns.store.get_archive_map("g1", ns.rid, "out")
    assert row["state"] == BridgeTaskState.DONE.value


@pytest.mark.asyncio
async def test_status_no_repair_when_polling_enabled(env):
    """interval>0（自动轮询）时 status 是纯 DB 读：不触 OpenList。"""
    ns = env
    ns.bridge._interval = 10
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    before = len(ns.client.submitted)
    st = await ns.bridge.status(ol_tid)
    assert st["state"] == BridgeTaskState.PENDING.value
    assert len(ns.client.submitted) == before  # 未发生任何控制面调用


@pytest.mark.asyncio
async def test_read_repair_pending_aggregate_converges_rows(env):
    """聚合读修复把所有 actionable out 行收敛；无变化行保持原状。"""
    ns = env
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    ns.client.undone.clear()
    ns.client.done_tasks[ol_tid] = OfflineTask(
        id=ol_tid,
        name="video.mp4",
        state="succeeded",
        status="done",
        progress=100.0,
        error="",
    )
    ns.client.files["/g1"] = [
        NetFile(name="video.mp4", size=1024, is_dir=False, modified=""),
    ]
    ns.bridge._interval = 0
    await ns.bridge.read_repair_pending()
    row = await ns.store.get_archive_map("g1", ns.rid, "out")
    assert row["state"] == BridgeTaskState.DONE.value
    # 再跑一次：已 done 的行不再 actionable，修复幂等
    await ns.bridge.read_repair_pending()
    assert ns.client.renames == []  # 目标名已正确，无需二次改名


@pytest.mark.asyncio
async def test_read_repair_missing_task_and_remote_stays_pending(env):
    """任务不在 undone/done 且远端文件未出现 → 行保持 pending（仍在重试窗口）。"""
    ns = env
    tid = await ns.bridge.submit_out("g1", ns.rid)
    await drain_op(ns.queue, tid)
    ol_tid = next(iter(ns.client.undone))
    ns.client.undone.clear()  # OpenList 列表里消失，远端也没有文件
    await ns.bridge.read_repair_row(
        await ns.store.get_archive_map("g1", ns.rid, "out")
    )
    row = await ns.store.get_archive_map("g1", ns.rid, "out")
    assert row["state"] == BridgeTaskState.PENDING.value
    assert row["task_id"] == ol_tid
