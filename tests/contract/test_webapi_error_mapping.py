"""`webapi.Bound` 的异常 → HTTP 状态码阶梯。

这层映射是插件里唯一决定"用户看到 4xx 还是 500"的地方，此前无测试：
`docs/接口契约.md` 一度写着"垃圾字符串 → 500"，而实际 `ValueError` 早已被
兜到 400 —— 没有测试就没有人核对过。

StoreUnavailable 一条是换库窗口（restore / rebuild）的回应用户可见契约：它是
暂态，必须给 503 而不是 500，否则前端与外部下载器会把"稍后重试"当成服务端故障。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.api_validate import ApiValidationError  # noqa: E402
from core.domain.enums import StoreUnavailable  # noqa: E402
from webapi.compatibility import Bound  # noqa: E402

_SERVICES = SimpleNamespace(ready=None)


def _bound_raising(exc: Exception):
    async def handler(_s):
        raise exc

    return Bound(_SERVICES, handler)


async def _status(exc: Exception) -> int:
    out = await _bound_raising(exc)()
    return out.get("status_code")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected",
    [
        (ApiValidationError("bad field"), 400),
        (ValueError("invalid literal for int()"), 400),
        (PermissionError("unauthorized"), 403),
        (FileNotFoundError("src.db"), 404),
        (StoreUnavailable("database is being swapped"), 503),
        (RuntimeError("connection blew up"), 500),
    ],
)
async def test_exception_ladder(exc, expected):
    assert await _status(exc) == expected


@pytest.mark.asyncio
async def test_store_unavailable_keeps_its_message():
    """503 的正文必须能告诉调用方"稍后重试"，不能退化成 generic 500 的固定文案。"""
    out = await _bound_raising(StoreUnavailable("restore aborted: calls still in flight"))()
    assert out.get("status_code") == 503
    assert "in flight" in str(out), out


@pytest.mark.asyncio
async def test_generic_runtime_error_is_still_500():
    """对照组：StoreUnavailable 的分支不能顺手把所有 RuntimeError 都变成 503。"""
    out = await _bound_raising(RuntimeError("connection manager is closed"))()
    assert out.get("status_code") == 500
