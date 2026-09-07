"""PlatformBotResolver 单元测试：三级回退 / 去重 / 重试停止 / best_bot 语义。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.platform import PlatformBotResolver  # noqa: E402


class _FakeAdapter:
    def __init__(self, bot=None):
        self.bot = bot


class _FakeInst:
    def __init__(self, bot=None):
        self.bot = bot


class _FakeManager:
    def __init__(self, insts):
        self.platform_insts = insts


class _FakeContext:
    def __init__(self, get_platform, platform_insts=None, fail_until=None):
        self._get_platform = get_platform
        self._calls = 0
        self._fail_until = fail_until
        self.platform_manager = _FakeManager(platform_insts or [])

    def get_platform(self, arg):
        self._calls += 1
        if self._fail_until is not None and self._calls <= self._fail_until:
            raise RuntimeError("adapter not ready")
        return self._get_platform(arg)


@pytest.mark.asyncio
async def test_all_paths_fail_returns_false():
    ctx = _FakeContext(lambda arg: None, fail_until=99)
    r = PlatformBotResolver(ctx)
    assert await r.resolve_once() is False
    assert r.best_bot() is None


@pytest.mark.asyncio
async def test_platform_adapter_bot_becomes_preferred():
    bot = object()
    ctx = _FakeContext(lambda arg: _FakeAdapter(bot))
    r = PlatformBotResolver(ctx)
    assert await r.resolve_once() is True
    assert r.preferred_bot is bot
    assert r.best_bot() is bot


@pytest.mark.asyncio
async def test_platform_insts_reflection_dedup_and_preferred_priority():
    preferred = object()
    extra = object()
    ctx = _FakeContext(
        lambda arg: _FakeAdapter(preferred),
        platform_insts=[_FakeInst(preferred), _FakeInst(extra)],
    )
    r = PlatformBotResolver(ctx)
    assert await r.resolve_once() is True
    assert r.bots == [preferred, extra]  # 去重：preferred 只出现一次
    assert r.best_bot() is preferred    # 平台适配器 bot 优先于反射 bot
    # 再探测一轮无新发现
    assert await r.resolve_once() is False


@pytest.mark.asyncio
async def test_ensure_retries_until_resolved():
    bot = object()
    ctx = _FakeContext(lambda arg: _FakeAdapter(bot), fail_until=2)
    r = PlatformBotResolver(ctx)
    assert await r.ensure(interval_sec=0.0, max_attempts=5) is True
    assert ctx._calls == 3  # 两次失败 + 一次成功（每轮探测最多触发一次 get_platform 异常路径）


@pytest.mark.asyncio
async def test_ensure_stops_when_already_resolved():
    ctx = _FakeContext(lambda arg: None, fail_until=99)
    r = PlatformBotResolver(ctx)
    r.bots.append(object())
    assert await r.ensure(interval_sec=0.0, max_attempts=2) is True
    assert ctx._calls == 0  # 已解析过：不再探测


@pytest.mark.asyncio
async def test_register_bot_switches_best_bot():
    ctx = _FakeContext(lambda arg: _FakeAdapter(object()))
    r = PlatformBotResolver(ctx)
    await r.resolve_once()
    event_bot = object()
    r.register_bot(event_bot)
    assert r.best_bot() is event_bot   # 最近事件 bot 最高优先
    r.register_bot(event_bot)          # 重复登记不产生重复条目
    assert r.bots.count(event_bot) == 1
