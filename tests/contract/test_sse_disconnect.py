"""SSE 断连释放回归（Bug 4）：客户端断开后 listener 必须被释放。

根因链：api_queue_events 的旧实现只在拿到下一个事件时才轮询 ASGI receive，
静默挂起的连接既不消费 http.disconnect，也永远不退出 →
listener 泄漏（每开一次页面 +1，重启才清零）+ 浏览器每主机 6 连接被占满，
其他 tab 的同源请求全部挂起 → 七个 tab 空白。

回归验证三件事：
1. 正常事件流：subscribe 迭代器收到事件后 aclose → listener 数回到基线；
2. 断连检测：模拟 receive 返回 http.disconnect 的请求，api_queue_events 的
   事件生成器在心跳窗口内返回（不再永久阻塞）；
3. finally 兜底：无论哪条路径退出，agen.aclose() 都会触发 subscribe 的
   finally → _listeners.discard。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


@pytest.mark.asyncio
async def test_events_generator_returns_on_http_disconnect():
    """http.disconnect 到达时 api_queue_events 的生成器在心跳窗口内返回。"""
    import webapi.events as ev_mod

    q = _queue()
    s = MagicMock()
    s.queue = q

    # 请求替身：receive 立即给出 http.disconnect
    req = MagicMock()
    req.receive = AsyncMock(return_value={"type": "http.disconnect"})

    captured = {}

    def _fake_stream_response(gen):
        # 与 conftest 的 stub 同形：同步接收生成器并原样返回
        captured["gen"] = gen
        return gen

    # events.py 从模块级 request/stream_response 取依赖 —— 直接替换
    orig_request, orig_stream = ev_mod.request, ev_mod.stream_response
    ev_mod.request = req
    ev_mod.stream_response = _fake_stream_response
    try:
        await ev_mod.api_queue_events(s)
        gen = captured["gen"]
        # 惰性启动：消费第一条 —— receive 已是 disconnect，生成器应立即
        # 返回（kind == "disconnected" → return），而非阻塞在事件等待
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=3)
        # finally 路径：subscribe 的 listener 已被释放
        assert len(q._listeners) == 0
    finally:
        ev_mod.request = orig_request
        ev_mod.stream_response = orig_stream
