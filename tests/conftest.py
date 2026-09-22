"""pytest 全局配置及宿主 API 测试桩。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _identity_decorator(*args, **kwargs):
    """兼容 AstrBot filter 装饰器的最小桩。"""
    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]
    return lambda func: func


# 使用真实 ModuleType 建立完整包层级；不能用 MagicMock 代替 package，
# 否则 import astrbot.api.star/web 会报「api 不是 package」。
_astrbot = ModuleType("astrbot")
_astrbot.__path__ = []
_api = ModuleType("astrbot.api")
_api.__path__ = []
_event = ModuleType("astrbot.api.event")
_star = ModuleType("astrbot.api.star")
_web = ModuleType("astrbot.api.web")

_api.logger = logging.getLogger("astrbot")
_event.AstrMessageEvent = object
_event.filter = SimpleNamespace(
    command=_identity_decorator,
    platform_adapter_type=_identity_decorator,
)
_star.Context = object
_star.Star = object

_web.request = MagicMock(name="request")
def _json_response(data):
    return data

def _error_response(message, status_code=400):
    return {"error": message, "status_code": status_code}

def _stream_response(body):
    return body

_web.json_response = _json_response
_web.error_response = _error_response
_web.stream_response = _stream_response
_web.PluginUploadFile = object
_web.PluginRequest = object
_web._request_var = None

_astrbot.api = _api
_api.event = _event
_api.star = _star
_api.web = _web
for _name, _module in (
    ("astrbot", _astrbot),
    ("astrbot.api", _api),
    ("astrbot.api.event", _event),
    ("astrbot.api.star", _star),
    ("astrbot.api.web", _web),
):
    sys.modules.setdefault(_name, _module)

import pytest  # noqa: E402  (after the stub modules above)

pytest_plugins = []


def pytest_collection_modifyitems(config, items):
    """标记 async 测试。"""
    for item in items:
        if "asyncio" in item.fixturenames:
            item.add_marker(pytest.mark.asyncio)


# ---- SKIP GUARD --------------------------------------------------------------
# 一条 skip 就是一处静默的覆盖率缺口：某个 importorskip 的依赖悄悄从环境里
# 消失，或者某个守卫条件永远不成立，都会让一次"全绿"的跑动实际变小而没人
# 察觉。2026-09-21 的审计发现 tests/unit 的 75 条 skip 里有 73 条其实是
# __init__.py 参数化过滤伪装成的 skip——它们既污染计数，又掩盖了真实盲区。
#
# REQUIRE_NO_SKIP=1（CI 设置）时，任何 skip 都让本次跑动失败。
_skipped: list[tuple[str, str]] = []


def pytest_runtest_logreport(report):
    if not report.skipped:
        return
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        reason = str(longrepr[2])
    else:
        reason = str(longrepr)
    _skipped.append((report.nodeid, reason.strip().splitlines()[-1]))


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("REQUIRE_NO_SKIP") != "1" or not _skipped:
        return
    counts: dict[str, int] = {}
    for _, reason in _skipped:
        counts[reason] = counts.get(reason, 0) + 1
    detail = "\n".join(
        f"  {n:>4} x {reason}"
        for reason, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    message = (
        f"\nREQUIRE_NO_SKIP=1 但有 {len(_skipped)} 条用例被跳过：\n{detail}\n"
        "请补上缺失的测试依赖（见 requirements-dev.txt）或删掉已失效的守卫。"
        "跳过不是通过，是覆盖率缺口。\n"
    )
    session.exitstatus = 1
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "SKIP GUARD", red=True)
        reporter.write_line(message)
    else:  # pragma: no cover - 非终端跑动
        sys.stderr.write(message)
