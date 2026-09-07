"""pytest 全局配置及宿主 API 测试桩。"""
from __future__ import annotations

import logging
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

import pytest

pytest_plugins = []


def pytest_collection_modifyitems(config, items):
    """标记 async 测试。"""
    for item in items:
        if "asyncio" in item.fixturenames:
            item.add_marker(pytest.mark.asyncio)
