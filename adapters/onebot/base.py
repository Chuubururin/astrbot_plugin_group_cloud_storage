"""NapCatBase —— NapCat 适配器底座：调用通道、限速、能力探测（docs/13 云端转义层）。"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from adapters.limiter.interval import IntervalLimiter
from core.domain.enums import CapabilityState, OneBotApiError, OneBotErrorKind
from core.log import logger

# 可能被判定为"unsupported"的异常信号
_UNSUPPORTED_HINTS = (
    "unsupported",
    "not found",
    "notfound",
    "no such action",
    "unknown action",
    "api not found",
    "404",
    "无此接口",
    "不支持",
    "method not exist",
)


class NapCatBase:
    def __init__(
        self,
        call_action: Callable[[str, dict], Awaitable[Any]],
        interval: float = 0.5,
    ):
        """
        Args:
            call_action: async (action, params) -> data（由 AstrBot OneBot 事件绑定，
                        典型实现：lambda action, p: await bot.call_action(action, **p)）
            interval: 扩展 API 请求最小间隔（秒）
        """
        self._call_action = call_action
        self._account_bot = None  # 多账号：显式绑定（扫描轮转）优先于注入回退链
        self._limiter = IntervalLimiter(interval)
        self._states: dict[str, CapabilityState] = {}
        self._lock = asyncio.Lock()

    def with_bot(self, bot) -> None:
        """多账号切换：显式绑定当前 bot（None=清除，回到注入回退链）。"""
        self._account_bot = bot

    # ---------- 能力探测 ----------

    def capability(self, action: str) -> CapabilityState:
        return self._states.get(action, CapabilityState.UNKNOWN)

    def _mark(self, action: str, state: CapabilityState) -> None:
        """记录能力状态；仅状态变化时打日志（避免批量扫描刷屏）。"""
        if self._states.get(action) == state:
            return
        self._states[action] = state
        logger.info(f"[group_cloud_storage] capability({action}) -> {state.value}")

    async def _call(self, action: str, **params) -> Any:
        """限速 + 调用 + 异常分类 + 能力状态更新。

        资源级/瞬态错误不标记能力、不做全局退避（统一交给 OpQueue 有限次重试），
        避免单个文件失败（如 URL 失效）导致全局能力挂起。
        """
        await self._limiter.acquire()
        try:
            if self._account_bot is not None:
                data = await self._account_bot.call_action(action, **params)
            else:
                data = await self._call_action(action, params)
        except OneBotApiError as e:
            if e.kind == OneBotErrorKind.LOCAL_ERROR:
                raise  # 本地环境态：不标记能力，调用方决定不重试
            self._classify(action, str(e), e)
        except Exception as e:
            self._classify(action, str(e), e)
        else:
            self._mark(action, CapabilityState.SUPPORTED)
            return data

    def _classify(self, action: str, msg: str, src: Exception) -> None:
        if any(h in msg.lower() for h in _UNSUPPORTED_HINTS):
            self._mark(action, CapabilityState.UNSUPPORTED)
            raise OneBotApiError(OneBotErrorKind.UNSUPPORTED, action, msg) from src
        if "timeout" in msg.lower():
            raise OneBotApiError(OneBotErrorKind.TIMEOUT, action, msg) from src
        raise OneBotApiError(OneBotErrorKind.REMOTE_ERROR, action, msg) from src

    async def close(self) -> None:
        pass
