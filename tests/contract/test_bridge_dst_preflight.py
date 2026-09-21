"""W-1 回归测试：openlist_dst_dir 挂载点预检。

实测依据（2026-09-21，容器内 OpenList，HTTP 均为 200）：

    POST /api/fs/get {"path":"/bridge-test"}     -> code=500
        "failed get storage: storage not found; rawPath: /bridge-test"
    POST /api/fs/get {"path":"/smb/nope/deep"}   -> code=500 "object not found"
    POST /api/fs/get {"path":"/smb"}             -> code=200

两条错误 message **都含** "not found"，而 `stat()` 曾用 `"not found" in msg`
判「文件不存在」⇒ 均返回 None，**无法区分「挂载点不存在」与「目录不存在」**。

因此预检**不建立在 `stat()` 的返回值上**，而是走语义单一的 `probe_mount()`。

> 注：`stat()` 本身已在 W-2 修复为「挂载错误抛错、对象缺失返 None」，
> 但预检仍走 `probe_mount()` —— 它把判定内聚在一个只读方法里，
> 不依赖调用方对异常语义的解读，且对 W-2 之外的措辞变化更鲁棒。
> `stat()` 的两种行为由本文件末尾两条用例分别钉住。

本文件钉的是**目标行为**：把「目标不在任何挂载点下」这条外部硬约束在启动时
暴露出来，且预检自身 fail-open（不得让插件加载失败）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import OpenListApiError  # noqa: E402
from adapters.external.openlist import OpenListClient  # noqa: E402
from core.application.bridge.service import BridgeService  # noqa: E402

# 与容器实测逐字一致的真实报错文案。
UNMOUNTED_MSG = "failed get storage: storage not found; rawPath: /bridge-test"
UNMOUNTED_MSG_DEEP = (
    "failed get storage: storage not found; rawPath: /bridge-test/950929451/x.bin"
)
MISSING_OBJECT_MSG = "object not found"


def _http_ok(payload):
    """真实形状：OpenList 用 HTTP 200 + 信封 code 表达错误。"""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def _client(payload_or_exc):
    client = OpenListClient(base_url="https://example.com:5244", token="t")
    http = AsyncMock()
    if isinstance(payload_or_exc, Exception):
        http.request.side_effect = payload_or_exc
    else:
        http.request.return_value = _http_ok(payload_or_exc)
    return client, patch.object(client, "_ensure_client", AsyncMock(return_value=http))


def _svc(dst_dir, probe_impl):
    """只装配预检所需字段的 BridgeService 替身（绕过 __init__ 的依赖装配）。"""
    svc = BridgeService.__new__(BridgeService)
    svc._client = MagicMock()
    svc._client.probe_mount = AsyncMock(side_effect=probe_impl)
    svc._dst_dir = dst_dir
    return svc


# ---------- probe_mount：判据本身 ----------

@pytest.mark.asyncio
async def test_probe_mount_rejects_a_path_outside_every_mount():
    """未挂载路径 -> (False, 原始 message)。"""
    client, patcher = _client({"code": 500, "message": UNMOUNTED_MSG, "data": None})
    with patcher:
        ok, reason = await client.probe_mount("/bridge-test")
    assert ok is False
    assert "storage not found" in (reason or "")


@pytest.mark.asyncio
async def test_probe_mount_accepts_a_mounted_path_whose_object_is_absent():
    """挂载点下的「对象不存在」是 mount 正常的证据 -> (True, None)。

    这条是 W-1 与 W-2 的分界：两者 message 都含 "not found"，
    必须靠 `failed get storage` 特征把挂载错误择出来。
    """
    client, patcher = _client({"code": 500, "message": MISSING_OBJECT_MSG, "data": None})
    with patcher:
        ok, reason = await client.probe_mount("/smb/nope/deep")
    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_probe_mount_accepts_a_live_mount():
    """正常挂载点（fs/get 200）-> (True, None)。"""
    client, patcher = _client(
        {"code": 200, "message": "success", "data": {"name": "smb", "is_dir": True}}
    )
    with patcher:
        ok, reason = await client.probe_mount("/smb")
    assert ok is True
    assert reason is None


# ---------- preflight_dst：聚合判据 ----------

@pytest.mark.asyncio
async def test_preflight_returns_false_when_dst_is_not_a_mount():
    """未挂载 -> False。"""
    async def _probe(path):
        if path == "/bridge-test":
            return False, UNMOUNTED_MSG
        return True, None

    svc = _svc("/bridge-test", _probe)
    assert await svc.preflight_dst() is False


@pytest.mark.asyncio
async def test_preflight_reports_the_first_broken_prefix(caplog):
    """深层未挂载路径：告警须点名出问题的前缀 + 两个修复方向。"""
    async def _probe(path):
        # 从浅到深：第一层 /bridge-test 就不归属任何挂载点。
        return False, UNMOUNTED_MSG_DEEP

    svc = _svc("/bridge-test/950929451", _probe)
    with caplog.at_level("ERROR"):
        ok = await svc.preflight_dst()

    assert ok is False
    text = caplog.text
    assert "preflight FAILED" in text
    assert "/bridge-test" in text
    assert "mount_path" in text
    assert "openlist_dst_dir" in text


@pytest.mark.asyncio
async def test_preflight_stops_at_the_first_unmounted_prefix():
    """从浅到深：第一层就不归属时立即停，不再向下白探。"""
    seen = []

    async def _probe(path):
        seen.append(path)
        return False, UNMOUNTED_MSG

    svc = _svc("/bridge-test/950929451", _probe)
    assert await svc.preflight_dst() is False
    assert seen == ["/bridge-test"]


@pytest.mark.asyncio
async def test_preflight_returns_true_on_the_first_owned_prefix():
    """深层 dst 但浅层前缀已被挂载点覆盖 -> 立即 True（不必逐层下探）。

    这也是唯一的「中间层」形态：挂载点一旦覆盖某前缀，其下所有层级都可归属
    （不存在的子目录由 mkdir 在挂载点内创建），所以不存在「中间某层断开」。
    """
    seen = []

    async def _probe(path):
        seen.append(path)
        return True, None

    svc = _svc("/smb/a/b/c/d", _probe)
    assert await svc.preflight_dst() is True
    assert seen == ["/smb"]


@pytest.mark.asyncio
async def test_preflight_returns_true_for_a_mounted_dst():
    """全链可达 -> True；探测路径归一（去尾斜杠）且健康场景只探一次。"""
    seen = []

    async def _probe(path):
        seen.append(path)
        return True, None

    svc = _svc("/smb/bridge-test/", _probe)
    assert await svc.preflight_dst() is True
    # /smb 命中即通过：根挂载点覆盖整棵子树，无需逐层下探。
    assert seen == ["/smb"]


# ---------- 只读 / fail-open / 空配置 ----------

@pytest.mark.asyncio
async def test_preflight_is_read_only():
    """预检不得触碰任何写入型 API（不在用户网盘里留痕迹）。"""
    async def _probe(path):
        return True, None

    svc = _svc("/smb/bridge-test", _probe)
    await svc.preflight_dst()
    # 健康路径：/smb 命中即通过，恰好一次只读探测。
    assert svc._client.probe_mount.await_count == 1
    assert not svc._client.mkdir.called
    assert not svc._client.submit_offline_download.called


@pytest.mark.asyncio
async def test_preflight_degrades_to_true_on_probe_errors():
    """预检自身遇到异常 -> 视为通过（fail-open，不得炸插件加载）。"""
    async def _probe(path):
        raise RuntimeError("network down")

    svc = _svc("/bridge-test", _probe)
    assert await svc.preflight_dst() is True


@pytest.mark.asyncio
async def test_preflight_skips_when_dst_dir_is_empty():
    """未配置 dst_dir -> 跳过，且不发生任何探测。"""
    async def _probe(path):  # pragma: no cover - 不应被调用
        raise AssertionError("probe_mount must not be called for empty dst")

    svc = _svc("", _probe)
    assert await svc.preflight_dst() is True
    assert svc._client.probe_mount.await_count == 0


@pytest.mark.asyncio
async def test_preflight_propagates_nothing_from_a_raising_probe():
    """probe 抛 OpenListApiError 也不得外泄（lifecycle 侧还有一层兜底，但这里先兜）。"""
    async def _probe(path):
        raise OpenListApiError("boom", code=500)

    svc = _svc("/bridge-test", _probe)
    assert await svc.preflight_dst() is True


# ---------- 不得误伤既有 stat() 语义 ----------

@pytest.mark.asyncio
async def test_stat_still_degrades_missing_object_to_none():
    """`object not found` 仍是 None —— stat() 的「对象不存在」契约不变。

    注意与 :func:`test_stat_raises_on_an_unmounted_path` 的分工：
    W-2 修复后，挂载错误抛错、对象缺失返 None，两者由此可分。
    """
    client, patcher = _client(
        {"code": 500, "message": MISSING_OBJECT_MSG, "data": None}
    )
    with patcher:
        assert await client.stat("/smb/bridge-test/x.bin") is None


@pytest.mark.asyncio
async def test_stat_raises_on_an_unmounted_path():
    """同一文案家族里的挂载错误必须抛（W-2 的核心判据）。"""
    client, patcher = _client({"code": 500, "message": UNMOUNTED_MSG, "data": None})
    with patcher:
        with pytest.raises(OpenListApiError):
            await client.stat("/bridge-test")
