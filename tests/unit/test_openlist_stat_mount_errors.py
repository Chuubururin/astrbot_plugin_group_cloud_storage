"""W-2 回归测试：stat() 不得把「挂载点不存在」吞成「文件不存在」。

实测依据（2026-09-21，容器内 OpenList，HTTP 均为 200）：

    POST /api/fs/get {"path":"/bridge-test"}     -> code=500
        "failed get storage: storage not found; rawPath: /bridge-test"
    POST /api/fs/get {"path":"/smb/nope/deep"}   -> code=500 "object not found"
    POST /api/fs/get {"path":"/smb"}             -> code=200

两条错误 message **都含** "not found"。旧 `stat()` 用 `"not found" in msg`
判「不存在」，于是**两者都返回 None** —— 挂载配置错误被伪装成正常「无此文件」。

后果（逐调用点核对，见 W-2 报告）：
- `polling.py:50`  误判「远端文件已删除」⇒ 清 archive_map 重新提交
- `polling.py:69`  误判「无冲突」⇒ 故障推迟到 Step 5 mkdir，且被判为**可重试**
- `recovery.py:41` 探针 None ⇒ **把任务标成 FAILED 并通知用户**（真实误报）
- `inbound.py:77`  源目录配错 ⇒ 静默认为「网盘里什么都没有」

本文件钉的是**目标行为**：挂载错误必须抛，对象缺失仍返回 None。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import OpenListApiError  # noqa: E402
from adapters.external.openlist import (  # noqa: E402
    _STORAGE_MARKERS,
    OpenListClient,
)

# 与容器实测逐字一致的真实报错文案。
UNMOUNTED_MSG = "failed get storage: storage not found; rawPath: /bridge-test"
UNMOUNTED_MSG_FILE = (
    "failed get storage: storage not found; rawPath: /bridge-test/950929451/x.bin"
)
MISSING_OBJECT_MSG = "object not found"


def _http_ok(payload):
    """真实形状：OpenList 用 HTTP 200 + 信封 code 表达错误。"""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def _client(payload):
    client = OpenListClient(base_url="https://example.com:5244", token="t")
    http = AsyncMock()
    http.request.return_value = _http_ok(payload)
    return client, patch.object(client, "_ensure_client", AsyncMock(return_value=http))


# ---------- 核心：挂载错误必须抛 ----------

@pytest.mark.asyncio
async def test_stat_raises_on_an_unmounted_directory():
    """未挂载的目录 -> 抛 OpenListApiError（旧码返回 None）。"""
    client, patcher = _client({"code": 500, "message": UNMOUNTED_MSG, "data": None})
    with patcher:
        with pytest.raises(OpenListApiError):
            await client.stat("/bridge-test")


@pytest.mark.asyncio
async def test_stat_raises_on_an_unmounted_file_path():
    """未挂载路径下的文件 -> 抛错（这是 bridge Step 4 的探测路径）。"""
    client, patcher = _client(
        {"code": 500, "message": UNMOUNTED_MSG_FILE, "data": None}
    )
    with patcher:
        with pytest.raises(OpenListApiError):
            await client.stat("/bridge-test/950929451/x.bin")


@pytest.mark.asyncio
async def test_stat_error_message_carries_the_raw_openlist_text():
    """抛出的错误须保留原始文案（运维据此定位挂载配置）。"""
    client, patcher = _client({"code": 500, "message": UNMOUNTED_MSG, "data": None})
    with patcher:
        with pytest.raises(OpenListApiError) as ei:
            await client.stat("/bridge-test")
    assert "failed get storage" in str(ei.value)


# ---------- 契约保持：对象不存在仍是 None ----------

@pytest.mark.asyncio
async def test_stat_still_returns_none_for_a_missing_object():
    """挂载点存在、对象不存在 -> None（既有契约，不得破坏）。"""
    client, patcher = _client(
        {"code": 500, "message": MISSING_OBJECT_MSG, "data": None}
    )
    with patcher:
        assert await client.stat("/smb/somewhere/x.bin") is None


@pytest.mark.asyncio
async def test_stat_still_returns_none_for_an_envelope_null_data():
    """信封 200 + data null -> None（既有 test_openlist 走的正是这条分支）。"""
    client, patcher = _client({"code": 200, "message": "success", "data": None})
    with patcher:
        assert await client.stat("/smb/missing.bin") is None


@pytest.mark.asyncio
async def test_stat_still_returns_none_for_a_404_style_error():
    """404 类文案仍归入「不存在」。"""
    client, patcher = _client({"code": 404, "message": "404 page not found", "data": None})
    with patcher:
        assert await client.stat("/smb/missing.bin") is None


@pytest.mark.asyncio
async def test_stat_still_returns_netfile_when_present():
    """正常存在 -> NetFile（回归保护）。"""
    client, patcher = _client(
        {
            "code": 200,
            "message": "success",
            "data": {
                "name": "file.zip",
                "size": 12345,
                "is_dir": False,
                "modified": "2026-09-21T00:00:00Z",
                "sign": "",
            },
        }
    )
    with patcher:
        got = await client.stat("/smb/file.zip")
    assert got is not None and got.name == "file.zip" and got.size == 12345


# ---------- 其它错误不得被吞 ----------

@pytest.mark.asyncio
async def test_stat_propagates_unrelated_api_errors():
    """与「不存在」无关的错误（如权限）必须继续抛，不被吞成 None。"""
    client, patcher = _client(
        {"code": 403, "message": "permission denied for this path", "data": None}
    )
    with patcher:
        with pytest.raises(OpenListApiError):
            await client.stat("/smb/secret.bin")


# ---------- 与 probe_mount 共用的特征常量 ----------

def test_storage_markers_cover_the_observed_openlist_wording():
    """特征常量必须覆盖实测文案（否则 W-1/W-2 同时失效）。"""
    msg = UNMOUNTED_MSG.lower()
    assert any(marker in msg for marker in _STORAGE_MARKERS)


def test_storage_markers_do_not_false_positive_on_a_plain_missing_object():
    """特征常量不得误伤「对象不存在」（否则正常的冲突检查会炸）。"""
    msg = MISSING_OBJECT_MSG.lower()
    assert not any(marker in msg for marker in _STORAGE_MARKERS)
