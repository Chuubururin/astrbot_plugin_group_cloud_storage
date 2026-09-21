"""H1 regression — every sync path must share ONE lock dict per group.

CloudIngestService used to build its own dict (IngestContext's
default_factory), so an ingest-triggered run_full_sync took a different
asyncio.Lock than a file_scan/sync op on the same group. The mutual
exclusion in ResourceSyncService.run_full_sync therefore never fired, and
two concurrent full syncs could mark each other's rows as missing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bootstrap import build_components  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from core.config.defaults import DEFAULTS  # noqa: E402
from core.domain.enums import SyncStatus  # noqa: E402
from core.domain.sync import SyncResult  # noqa: E402


def _cfg() -> dict:
    return dict(DEFAULTS)


def test_bootstrap_wires_one_lock_dict_into_every_sync_path(tmp_path):
    """End-to-end wiring: ingest / file-ops / Services.lock_for share one dict."""
    comps = build_components(
        bind_call_action=lambda *a, **k: None,
        run_handler=lambda op: None,
        ready=lambda: None,
        config=_cfg(),
        data_dir=tmp_path,
    )
    ops = comps["ops"]
    ingest = comps["ingest"]
    services = comps["services"]
    assert ops._sync_locks is services.sync_locks
    assert ingest._sync_locks is services.sync_locks, (
        "ingest must share the lock dict: a private dict lets an ingest-triggered "
        "sync race a file_scan/sync op on the same group"
    )


@pytest.mark.asyncio
async def test_same_group_syncs_serialize_across_services(tmp_path):
    """行为断言：ingest 与 file-ops 在同一群上真的互斥。

    旧断言用同一个传入 dict 调两次 setdefault 再比对象，只证明“构造器存下了入参”。
    这里让两个服务各自按生产写法（``_sync_locks.setdefault(target, Lock())``）
    取锁，再用真实的 ResourceSyncService.run_full_sync 互斥逻辑去竞争：
    反向验证——把 CloudIngestService 退回私有字典（忽略注入），第二个同步会拿到
    另一把锁、不被拒，wait_for 超时 → 本用例失败。
    """
    shared: dict[str, asyncio.Lock] = {}
    ingest = CloudIngestService(
        api=MagicMock(), store=MagicMock(), queue=MagicMock(), sync=MagicMock(),
        tmp_dir=tmp_path / "tmp", config=_cfg(), sync_locks=shared,
    )
    ops = FileOpsService(
        api=MagicMock(), store=MagicMock(), queue=MagicMock(), sync=MagicMock(),
        tmp_dir=tmp_path / "tmp", config=_cfg(), sync_locks=shared,
    )

    # 只借用 run_full_sync 的互斥壳（lock.locked() 检查 + async with），
    # _sync_unlocked 换成阻塞探针，以便观察是否真的串行。
    sync = object.__new__(ResourceSyncService)
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def _slow(group_id: str) -> SyncResult:
        order.append("enter")
        entered.set()
        await release.wait()
        order.append("exit")
        return SyncResult(status=SyncStatus.OK)

    sync._sync_unlocked = _slow  # type: ignore[method-assign]

    first = asyncio.ensure_future(
        sync.run_full_sync("g1", ingest._sync_locks.setdefault("g1", asyncio.Lock()))
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = await asyncio.wait_for(
            sync.run_full_sync("g1", ops._sync_locks.setdefault("g1", asyncio.Lock())), 2
        )
        assert second.status == SyncStatus.FAILED
        assert "already running" in (second.error or "")
        assert order == ["enter"], "同一群的第二个同步必须被拒，不得并发执行"
    finally:
        release.set()
        first_result = await asyncio.wait_for(first, 2)
    assert first_result.status == SyncStatus.OK
    assert order == ["enter", "exit"]


def test_lock_dict_is_optional_for_backward_compatibility(tmp_path):
    ingest = CloudIngestService(
        api=MagicMock(), store=MagicMock(), queue=MagicMock(), sync=MagicMock(),
        tmp_dir=tmp_path / "tmp", config=_cfg(),
    )
    assert isinstance(ingest._sync_locks, dict)
