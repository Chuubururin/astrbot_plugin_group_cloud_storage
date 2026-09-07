"""bootstrap 装配契约测试：组件键完整、依赖注入正确、config 包装、SQLite 工厂分支。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Mock astrbot.api before importing bootstrap（模块级导入安全）
astrbot_mock = MagicMock()
sys.modules.setdefault("astrbot", MagicMock())
sys.modules.setdefault("astrbot.api", astrbot_mock)
sys.modules.setdefault("astrbot.api.logger", astrbot_mock.logger)

from bootstrap import build_components  # noqa: E402
from core.config import PluginConfig  # noqa: E402
from core.application.queue.op import DEFAULT_HIGH_PRIORITY  # noqa: E402

EXPECTED_KEYS = {
    "store", "api", "perm", "sync", "limiter", "queue", "scan", "ops",
    "transfer", "ingest", "dlserver", "gateway", "bridge", "openlist_client",
    "task_control", "services", "auto_scan_hours", "database_admin",
}


@pytest.fixture
def ready_calls():
    return {"n": 0}


async def _fake_ready():
    pass


def _fake_handlers():
    async def fake_bind(action, params):
        return {}

    async def fake_handler(op):
        pass

    async def fake_ready():
        pass

    return fake_bind, fake_handler, fake_ready


def test_build_components_keys_and_wiring(tmp_path, ready_calls):
    async def fake_ready():
        ready_calls["n"] += 1

    async def fake_bind(action, params):
        return {}

    async def fake_handler(op):
        pass

    comps = build_components(
        bind_call_action=fake_bind,
        run_handler=fake_handler,
        ready=fake_ready,
        config={"request_interval_ms": 100, "managed_groups": ["g1"]},
        data_dir=tmp_path,
    )
    assert set(comps) == EXPECTED_KEYS
    assert comps["services"].ready is fake_ready
    assert comps["services"].store is comps["store"]
    assert comps["services"].queue is comps["queue"]
    # 配置包装：services.config 为 PluginConfig，get 透传
    assert isinstance(comps["services"].config, PluginConfig)
    assert comps["services"].config.get("request_interval_ms", 500) == 100
    assert comps["services"].config.get("missing", "d") == "d"
    assert comps["auto_scan_hours"] == 6.0  # schema 默认
    # 队列优先级集合来自 config（未配置 → 内置默认）
    assert comps["queue"]._high_priority is DEFAULT_HIGH_PRIORITY


def test_build_components_priority_override(tmp_path):
    bind, handler, ready = _fake_handlers()
    comps = build_components(
        bind_call_action=bind,
        run_handler=handler,
        ready=ready,
        config={"op_high_priority_kinds": ["rename", "upload"]},
        data_dir=tmp_path,
    )
    assert comps["queue"]._high_priority == {"rename", "upload"}


# ---------- SQLite 工厂分支（独立于 astrbot 运行） ----------


def test_sqlite_default(tmp_path):
    bind, handler, ready = _fake_handlers()
    comps = build_components(
        bind_call_action=bind,
        run_handler=handler,
        ready=ready,
        config={},
        data_dir=tmp_path,
    )
    from adapters.persistence.sqlite import SqliteMetaStore
    assert isinstance(comps["store"], SqliteMetaStore)


def test_sqlite_explicit(tmp_path):
    bind, handler, ready = _fake_handlers()
    comps = build_components(
        bind_call_action=bind,
        run_handler=handler,
        ready=ready,
        config={"storage_mode": "sqlite"},
        data_dir=tmp_path,
    )
    from adapters.persistence.sqlite import SqliteMetaStore
    assert isinstance(comps["store"], SqliteMetaStore)
