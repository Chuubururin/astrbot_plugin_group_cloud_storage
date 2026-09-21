"""SSE 断连释放回归（Bug 4）：客户端断开后 listener 必须被释放。

根因链：api_queue_events 的旧实现只在拿到下一个事件时才轮询 ASGI receive，
静默挂起的连接既不消费 http.disconnect，也永远不退出 →
listener 泄漏（每开一次页面 +1，重启才清零）+ 浏览器每主机 6 连接被占满，
其他 tab 的同源请求全部挂起 → 七个 tab 空白。

回归验证三件事：
1. 正常事件流：subscribe 迭代器收到事件后 aclose → listener 数回到基线；
2. 断连检测：按宿主真实代理形状（PluginRequestProxy → PluginRequest._request）
   提供 receive=http.disconnect，api_queue_events 的生成器在心跳窗口内返回；
3. finally 兜底：无论哪条路径退出，agen.aclose() 都会触发 subscribe 的
   finally → _listeners.discard。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.queue import OpQueue  # noqa: E402


def _queue() -> OpQueue:
    # OpQueue 构造参数：run_handler 必传；事件切片不依赖其余依赖
    return OpQueue(lambda op: asyncio.sleep(0, result=None))


@pytest.mark.asyncio
async def test_subscribe_releases_listener_on_close():
    q = _queue()
    assert len(q._listeners) == 0
    agen = q.subscribe()
    # async generator 惰性：先启动迭代让 subscribe 注册 listener，再 publish
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    assert len(q._listeners) == 1
    q.publish({"type": "t"})
    ev = await asyncio.wait_for(task, timeout=2)
    assert ev["type"] == "t"
    await agen.aclose()
    assert len(q._listeners) == 0


class _StarletteRequestLike:
    """Starlette Request 最小替身：ASGI 可调用挂在公开的 receive 上。"""

    def __init__(self, receive):
        self.receive = receive


class _PluginRequestLike:
    """宿主 PluginRequest 替身：真正的请求对象在 _request 上。"""

    def __init__(self, receive):
        self._request = _StarletteRequestLike(receive)


class _PluginRequestProxyLike:
    """宿主 PluginRequestProxy 替身：自己没有 receive，只做 __getattr__ 转发。"""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, key):
        return getattr(self._target, key)


@pytest.mark.asyncio
async def test_events_generator_returns_on_http_disconnect(monkeypatch):
    """http.disconnect 到达时 api_queue_events 的生成器在心跳窗口内返回。

    替身按宿主真实形状搭建：PluginRequestProxy 上没有 receive 属性，只能通过
    __getattr__ 转发到 PluginRequest._request。修复前 events.py 只探 request.receive，
    getattr 恒得 None —— 文档所述的断连检测从未生效。
    """
    import webapi.events as ev_mod

    q = _queue()
    s = MagicMock()
    s.queue = q

    async def _disconnect():
        return {"type": "http.disconnect"}

    proxy = _PluginRequestProxyLike(_PluginRequestLike(_disconnect))

    captured = {}

    def _fake_stream_response(gen):
        # 与 conftest 的 stub 同形：同步接收生成器并原样返回
        captured["gen"] = gen
        return gen

    monkeypatch.setattr(ev_mod, "request", proxy)
    monkeypatch.setattr(ev_mod, "stream_response", _fake_stream_response)

    await ev_mod.api_queue_events(s)
    gen = captured["gen"]
    # 惰性启动：消费第一条 —— receive 已是 disconnect，生成器应立即
    # 返回，而非阻塞在事件等待
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=3)
    # finally 路径：subscribe 的 listener 已被释放
    assert len(q._listeners) == 0


def test_asgi_receive_probe_walks_host_proxy(monkeypatch):
    """探针必须穿过宿主代理拿到 ASGI receive；拿不到时返回 None。"""
    import webapi.events as ev_mod

    async def _recv():
        return {"type": "http.request"}

    # 宿主真实形状：代理 → PluginRequest → _request(Starlette).receive
    monkeypatch.setattr(
        ev_mod, "request", _PluginRequestProxyLike(_PluginRequestLike(_recv))
    )
    assert ev_mod._asgi_receive() is _recv
    # 代理直接带 receive 的旧式替身仍可用
    monkeypatch.setattr(ev_mod, "request", SimpleNamespace(receive=_recv))
    assert ev_mod._asgi_receive() is _recv
    # 完全不可达 → None（降级为心跳 + 框架取消，不得抛异常）
    monkeypatch.setattr(
        ev_mod, "request", _PluginRequestProxyLike(SimpleNamespace())
    )
    assert ev_mod._asgi_receive() is None


@pytest.mark.asyncio
async def test_events_survive_heartbeat_window(monkeypatch):
    """坏链#25 回归：心跳窗口超时不得关闭订阅生成器（wait_for 取消
    __anext__ 会注入 CancelledError 并触发 subscribe 的 finally ——
    旧实现由此在第一个心跳窗口后永久失聪），窗口之后的事件仍须送达。"""
    import webapi.events as ev_mod

    monkeypatch.setattr(ev_mod, "SSE_HEARTBEAT_SEC", 0.1)

    q = _queue()
    s = MagicMock()
    s.queue = q

    async def _slow_receive():
        # 长挂起的 receive：整条用例内不触发断连
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    req = MagicMock()
    req.receive = _slow_receive

    captured = {}

    def _fake_stream_response(gen):
        captured["gen"] = gen
        return gen

    monkeypatch.setattr(ev_mod, "request", req)
    monkeypatch.setattr(ev_mod, "stream_response", _fake_stream_response)

    await ev_mod.api_queue_events(s)
    gen = captured["gen"]
    try:
        # 窗口内无事件 → 心跳，而非流终止
        first = await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert '"heartbeat"' in first
        # 心跳之后订阅必须仍然存活：publish 的事件照常送达
        pub = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        assert len(q._listeners) == 1
        q.publish({"type": "done", "task_id": "x"})
        line = await asyncio.wait_for(pub, timeout=2)
        assert line.startswith("data: ")
        assert json.loads(line[len("data: "):])["type"] == "done"
    finally:
        await gen.aclose()
    assert len(q._listeners) == 0
