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
