"""WebAPI 输入加固回归：非法参数形状必须落成 400 / 逐项失败，不得穿透成 500。

覆盖（均为“请求体形状非法”这一类）：
- M15 essence/distribute 未过群开闸 → 离线账号/解散群仍能提交转存任务
- groups/system-msg 空群号（best_bot 兜底下执行）
- groups/batch items 缺 group_id（_group_open_error(s, "") 短路）
- files/batch-* 的 items 非对象元素 → pick() 抛 TypeError
- netdisk/rename-batch 的 renames 非对象元素 → .get 抛 AttributeError
- files/download 的 allow_incomplete 未小写化（降级下载被静默忽略）

Run: pytest tests/contract/test_webapi_input_hardening.py -v
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapi.groups as groups_mod  # noqa: E402
import webapi.netdisk_mutation as netdisk_mod  # noqa: E402
import webapi.resources_mutation as res_mod  # noqa: E402
import webapi.webapi_ext as ext_mod  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402


def _json_response(data):
    return data


def _error_response(msg, status_code=400):
    return {"status": "error", "message": msg, "status_code": status_code}


_DEFAULT_SEAMS = ("_param", "_group_open_error")


def _patch_io(monkeypatch, module, *, body=None, params=None, gate=None,
              seams=_DEFAULT_SEAMS):
    """把模块里的 request 读取/响应/开闸接缝换成内存替身。

    ``seams`` 是“本模块真的会用到的”可选接缝，必须在调用处显式声明：
    setattr 无条件执行，接缝改名/删除会直接 AttributeError（fail loud）。
    之前用 hasattr 探测，接缝一旦改名打桩就静默失效——``gate=`` 被无声忽略，
    用例仍绿，但实际什么都没验证。
    """
    async def _json_body():
        return body if isinstance(body, dict) else {}

    async def _param(key, default=""):
        value = (params or {}).get(key)
        return str(value) if value is not None else default

    async def _gate(s, group):
        return (gate or {}).get(str(group))

    monkeypatch.setattr(module, "json_body", _json_body)
    monkeypatch.setattr(module, "json_response", _json_response)
    monkeypatch.setattr(module, "error_response", _error_response)
    replacements = {"_param": _param, "_group_open_error": _gate}
    for name in seams:
        monkeypatch.setattr(module, name, replacements[name])


# ---------------------------------------------------------------------------
# M15: essence/distribute 必须与 files/albums/netdisk 一样过开闸
# ---------------------------------------------------------------------------

class _RecordingDistributor:
    def __init__(self):
        self.calls: list[tuple] = []

    async def distribute_essence(self, group, rid, target):
        self.calls.append((group, rid, target))
        return {"ok": True, "group": group, "id": rid}


@pytest.mark.asyncio
async def test_essence_distribute_blocked_when_group_not_openable(monkeypatch):
    """群归属账号离线/群已解散时不得提交转存任务（反向验证：修复前会提交）。"""
    dist = _RecordingDistributor()
    s = SimpleNamespace(distributor=dist, ready=None)
    _patch_io(
        monkeypatch, ext_mod,
        params={"group": "g-off"},
        body={"id": 7, "target": "local"},
        gate={"g-off": _error_response("群归属账号离线，暂不可操作", 403)},
    )
    out = await ext_mod.api_essence_distribute(s)
    assert out.get("status_code") == 403
    assert dist.calls == []


@pytest.mark.asyncio
async def test_essence_distribute_open_group_still_submits(monkeypatch):
    dist = _RecordingDistributor()
    s = SimpleNamespace(distributor=dist, ready=None)
    _patch_io(
        monkeypatch, ext_mod,
        params={"group": "g-on"},
        body={"id": 7, "target": "local"},
        gate={},
    )
    out = await ext_mod.api_essence_distribute(s)
    assert dist.calls == [("g-on", 7, "local")]
    assert out.get("ok") is True


# ---------------------------------------------------------------------------
# 低危：groups/system-msg 空群号
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_system_msg_requires_group(monkeypatch):
    calls: list[str] = []

    class _Api:
        async def get_group_system_msg(self, group, only_pending, count):
            calls.append(group)
            return []

    s = SimpleNamespace(api=_Api(), ready=None)
    _patch_io(monkeypatch, groups_mod, params={}, gate={})
    out = await groups_mod.api_group_system_msg(s)
    assert out.get("status_code") == 400
    assert calls == []


# ---------------------------------------------------------------------------
# 低危：groups/batch items 缺 group_id / 非对象
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[{}], [{"group_id": ""}], [123], ["g1"]])
async def test_groups_batch_update_rejects_item_without_group_id(monkeypatch, items):
    submitted: list[tuple] = []

    class _Queue:
        async def submit(self, action, target, payload):
            submitted.append((action, target, payload))
            return "t1"

    s = SimpleNamespace(queue=_Queue(), store=SimpleNamespace(), ready=None)
    _patch_io(
        monkeypatch, groups_mod,
        body={"items": items},
        gate={},
    )
    out = await groups_mod.api_groups_batch_update(s)
    assert out.get("status_code") == 400
    # 反向验证：修复前 target="" 的 rename 任务会被提交，执行期才失败
    assert submitted == []


# ---------------------------------------------------------------------------
# 低危：files/batch-* 的 items 非对象元素
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_files_batch_delete_non_object_item_is_per_item_failure(monkeypatch):
    submitted: list[tuple] = []

    class _Ops:
        async def submit_delete(self, gid, fid):
            submitted.append((gid, fid))

    s = SimpleNamespace(ops=_Ops(), ready=None)
    _patch_io(
        monkeypatch, res_mod,
        body={"items": [123, {"id": 1, "group": "g1"}]},
        gate={},
    )
    out = await res_mod.api_files_batch_delete(s)
    assert submitted == [("g1", 1)]
    assert out["submitted"] == 1
    assert len(out["failed"]) == 1
    assert "object" in out["failed"][0]


# ---------------------------------------------------------------------------
# 低危：netdisk/rename-batch 的 renames 非对象元素
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_netdisk_rename_batch_non_object_item(monkeypatch):
    renamed: list[tuple] = []

    class _Bridge:
        async def rename(self, path, name):
            renamed.append((path, name))

    s = SimpleNamespace(bridge=_Bridge(), ready=None)
    _patch_io(
        monkeypatch, netdisk_mod,
        body={"renames": [123, {"path": "/a", "name": "b"}]},
        seams=(),  # netdisk_mutation 不导入 _param/_group_open_error
    )
    out = await netdisk_mod.api_netdisk_rename_batch(s)
    assert renamed == [("/a", "b")]
    assert out["ok"] is False
    assert any("Invalid item" in e for e in out["errors"])


# ---------------------------------------------------------------------------
# 低危：files/download 的 allow_incomplete 大小写
# ---------------------------------------------------------------------------

@pytest.fixture
def _fastapi_stub(monkeypatch):
    """测试环境未装 fastapi；files/download 只在构造响应时才需要它。"""
    fastapi = types.ModuleType("fastapi")
    fastapi.__path__ = []
    responses = types.ModuleType("fastapi.responses")

    class StreamingResponse:
        def __init__(self, *args, **kwargs):
            pass

    responses.StreamingResponse = StreamingResponse
    fastapi.responses = responses
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)


@pytest.mark.asyncio
async def test_file_download_allow_incomplete_is_case_insensitive(monkeypatch, _fastapi_stub):
    """?allow_incomplete=True 必须被识别（反向验证：修复前被判为 False）。"""
    seen: list[bool] = []

    class _Ops:
        async def download_info(self, group, fid, allow_incomplete=False):
            seen.append(allow_incomplete)
            raise ValueError("volume part missing")

    class _Store:
        async def get_resource_any(self, fid):
            return None

    s = SimpleNamespace(ops=_Ops(), store=_Store(), ready=None)
    _patch_io(
        monkeypatch, res_mod,
        params={"group": "g1", "id": "5", "allow_incomplete": "True"},
        gate={},
    )
    out = await res_mod.api_file_download(s)
    assert seen == [True]
    assert out.get("status_code") == 404


# ---------------------------------------------------------------------------
# 打桩接缝必须 fail loud（seam 改名不得静默失效）
# ---------------------------------------------------------------------------


def test_patch_io_fails_loud_when_seam_is_missing(monkeypatch):
    """模块缺少声明的接缝时必须报错，而不是静默跳过打桩。

    反向验证：旧版用 hasattr 探测，这里不会报错（gate= 被无声忽略），用例绿。
    """
    bare = types.ModuleType("bare_seam_module")
    # 三个必填接缝先给上，才能确定失败确实来自声明的可选接缝缺失
    bare.json_body = None
    bare.json_response = None
    bare.error_response = None
    with pytest.raises(AttributeError):
        _patch_io(monkeypatch, bare, gate={"g": {"status_code": 403}})


# ---------------------------------------------------------------------------
# M4: SSE 断连监视必须在 body-less GET 的首条 http.request 之后继续重臂
# ---------------------------------------------------------------------------


class _StarletteReqLike:
    """Starlette Request 最小替身：ASGI 可调用挂在公开的 receive 上。"""

    def __init__(self, receive):
        self.receive = receive


@pytest.mark.asyncio
async def test_sse_disconnect_detected_after_bodyless_get_request(monkeypatch):
    """生产真实形状：首条 receive() 是 {"http.request", more_body=False}，
    随后才是 http.disconnect。

    修复前该 http.request 被当成终止信号（recv_task=None），从此不再 receive()
    ——只有“首条消息就是 disconnect”才生效，主路径上断连检测是死代码。
    反向验证：还原 more_body 条件后本用例在 ``len(calls) >= 2`` 处失败。
    """
    import webapi.events as ev_mod

    monkeypatch.setattr(ev_mod, "SSE_HEARTBEAT_SEC", 0.1)

    q = OpQueue(lambda op: asyncio.sleep(0, result=None))
    s = SimpleNamespace(queue=q)

    calls: list[dict] = []
    disconnect_now = asyncio.Event()

    async def _receive():
        # 调用计数在入口处记录：第二次调用会阻塞在 disconnect_now 上，
        # 计数必须能证明“重新 arm 了 receive()”而非证明“拿到了消息”。
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            # uvicorn h11_impl 对无 body 的 GET 的首条消息
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    monkeypatch.setattr(ev_mod, "request", _StarletteReqLike(_receive))

    gen = await ev_mod.api_queue_events(s)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert '"heartbeat"' in first
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 2, (
            "http.request(more_body=False) 之后必须重新 arm receive()，"
            "否则 http.disconnect 永远拿不到"
        )
        disconnect_now.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=2)
    finally:
        await gen.aclose()
    # finally 路径：listener 已释放
    assert len(q._listeners) == 0
