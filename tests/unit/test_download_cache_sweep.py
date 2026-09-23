"""下载服务缓存治理（P2-11）：容量上限 / 闲置清理 / 命中日志。

背景：``DownloadServerService`` 的缓存根是 ``tempfile.mkdtemp``，
**唯一的清理是 ``shutdown()`` 的 rmtree** ⇒ 长期运行的机器人一直往 /tmp 里堆，
直到插件重载。建议书说这条「只做局部」：本轮只做容量上限 + 闲置清理 + 命中日志，
不移动文件、不引入大范围抽象。

安全边界（见 ``core/application/download_cache.py``）：
只碰两个缓存目录、**永不碰 ``*.part``**（写入方的暂存文件，删掉会截断进行中的
下载）、``CACHE_SWEEP_GRACE`` 秒内被动过的文件一律保留。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time

import pytest

from core.application import download_cache as dcache
from core.application import download_server_io as dio
from core.application.download_server import DownloadServerService

OLD = 7200.0  # 2 小时前


class _NullStore:
    """DownloadServerService 只保存它，不调用。"""


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    """构造服务实例，并把 mkdtemp 缓存根重定向到 tmp_path（用后清理）。"""
    created: list[DownloadServerService] = []
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda prefix="": str(tmp_path / f"cs-{len(created)}")
    )

    def _make(**config):
        svc = DownloadServerService(_NullStore(), dict(config))
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        if svc._cache_root is not None:  # shutdown() 会把它置 None
            shutil.rmtree(svc._cache_root, ignore_errors=True)


def _write(path, payload: bytes, *, age: float) -> None:
    path.write_bytes(payload)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


# ---------------- 配置 → 预算 ----------------


def test_config_keys_drive_the_budgets(make_service):
    """两个新键换算成字节 / 秒；0 表示关闭该规则。"""
    svc = make_service(download_cache_max_mb=7, download_cache_ttl_hours=2)
    assert svc.cache_max_bytes == 7 * 1024 * 1024
    assert svc.cache_ttl_seconds == 2 * 3600

    off = make_service(download_cache_max_mb=0, download_cache_ttl_hours=0)
    assert off.cache_max_bytes == 0
    assert off.cache_ttl_seconds == 0


# ---------------- 闲置清理 ----------------


def test_sweep_drops_idle_files_and_keeps_fresh_ones(make_service):
    svc = make_service(download_cache_ttl_hours=1)
    idle = svc._cache_dir / "idle.bin"
    fresh = svc._cache_dir / "fresh.bin"
    _write(idle, b"x" * 10, age=OLD)
    _write(fresh, b"y" * 10, age=0.0)

    freed = dcache.sweep_cache(svc)

    assert freed == 10, f"应只回收闲置文件，实际 {freed}"
    assert not idle.exists(), "闲置文件必须被清掉"
    assert fresh.exists(), "刚用过的文件不得被清掉"


def test_sweep_is_a_noop_when_both_rules_are_off(make_service):
    svc = make_service(download_cache_max_mb=0, download_cache_ttl_hours=0)
    idle = svc._cache_dir / "idle.bin"
    _write(idle, b"x" * 10, age=OLD)

    assert dcache.sweep_cache(svc) == 0
    assert idle.exists(), "两条规则都关掉时不得清理任何文件"


# ---------------- 容量上限（LRU） ----------------


def test_sweep_evicts_lru_until_under_quota(make_service):
    svc = make_service(download_cache_max_mb=0)
    svc.cache_max_bytes = 100  # 字节级上限，便于精确断言
    oldest = svc._cache_dir / "a.bin"
    middle = svc._cache_dir / "b.bin"
    newest = svc._cache_dir / "c.bin"
    _write(oldest, b"a" * 60, age=3 * OLD)
    _write(middle, b"b" * 60, age=2 * OLD)
    _write(newest, b"c" * 60, age=OLD)

    freed = dcache.sweep_cache(svc)

    remaining = [p for p in (oldest, middle, newest) if p.exists()]
    assert sum(p.stat().st_size for p in remaining) <= 100, "逐出后必须落到上限之内"
    assert not oldest.exists(), "应优先逐出最久未使用的条目"
    assert newest.exists(), "最新的条目必须保留"
    assert freed == 120, f"应回收两个 60B 文件，实际 {freed}"


# ---------------- 安全边界 ----------------


def test_sweep_never_touches_part_files_or_outside_paths(make_service, tmp_path):
    svc = make_service(download_cache_ttl_hours=1)
    part = svc._cache_dir / "in-flight.part"
    _write(part, b"z" * 10, age=OLD)
    outside = tmp_path / "not-cache.bin"
    _write(outside, b"w" * 10, age=OLD)

    dcache.sweep_cache(svc)

    assert part.exists(), "*.part 是写入方的暂存文件，删掉会截断进行中的下载"
    assert outside.exists(), "缓存目录之外的文件一律不碰"


# ---------------- 走真实调用路径的守卫 ----------------


def test_materialize_sweeps_the_cache_and_logs_hits(make_service, monkeypatch):
    """清理必须真的被物化流程触发（否则它只是死代码），命中必须留痕。"""
    svc = make_service(download_cache_ttl_hours=1)
    monkeypatch.setattr(svc, "_run_in_loop", lambda coro: asyncio.run(coro))

    src = svc._cache_root / "recon_source.bin"
    src.write_bytes(b"payload")

    async def _download_info(group, rid):
        return (src.as_posix(), "file.bin")

    svc._download_info = _download_info

    idle = svc._cache_dir / "idle.bin"
    _write(idle, b"x" * 10, age=OLD)

    info = {"group": "g1", "id": 1, "name": "file.bin", "size": 7}
    first = dio.materialize_to_cache(svc, info)

    # 反空断言：物化本身必须成功，否则"清理发生了"毫无意义。
    assert first.exists(), f"物化未产出缓存文件: {first}"
    assert first.read_bytes() == b"payload"
    assert not idle.exists(), "物化流程必须触发缓存清理（否则清理只是死代码）"

    # 命中：第二次不再下载，并留下命中日志。
    seen: list[str] = []
    monkeypatch.setattr(
        dio.logger, "debug", lambda msg, *a, **k: seen.append(str(msg))
    )
    again = dio.materialize_to_cache(svc, info)
    assert again == first
    assert any("cache hit" in m for m in seen), f"缓存命中必须留痕，实际 {seen}"
