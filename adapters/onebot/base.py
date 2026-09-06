"""NapCatBase -- NapCat adapter foundation: call channel, rate limiting,
and capability probing."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from adapters.limiter.interval import IntervalLimiter
from adapters.limiter.tier import interval_mult
from core.domain.enums import CapabilityState, OneBotApiError, OneBotErrorKind
from core.log import logger

# Exception message hints treated as "unsupported"
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
        """Initialize the adapter base.

        Args:
            call_action: async (action, params) -> data, bound to the AstrBot
                OneBot event; typical implementation:
                lambda action, p: await bot.call_action(action, **p)
            interval: minimum interval between extension API requests (seconds)
        """
        self._call_action = call_action
        self._account_bot = None  # explicit binding (scan rotation) wins over the injected chain
        self._limiter = IntervalLimiter(interval)
        self._states: dict[str, CapabilityState] = {}
        self._lock = asyncio.Lock()

    def with_bot(self, bot) -> None:
        """Multi-account switch: bind the current bot explicitly
        (None clears the binding and returns to the injected fallback chain)."""
        self._account_bot = bot

    # ---------- Capability probing ----------

    def capability(self, action: str) -> CapabilityState:
        return self._states.get(action, CapabilityState.UNKNOWN)

    def _mark(self, action: str, state: CapabilityState) -> None:
        """Record capability state; log only on state transitions (avoids
        log spam during batch scans)."""
        if self._states.get(action) == state:
            return
        self._states[action] = state
        logger.info(f"[group_cloud_storage] capability({action}) -> {state.value}")

    async def _call(self, action: str, **params) -> Any:
        """Rate-limit, call, classify exceptions, and update capability state.

        Resource-level/transient errors neither mark capability nor trigger
        global backoff (bounded retries are handled uniformly by OpQueue),
        so a single file failure (e.g. an expired URL) cannot suspend the
        whole capability.
        """
        await self._limiter.acquire(mult=interval_mult(action))
        try:
            if self._account_bot is not None:
                data = await self._account_bot.call_action(action, **params)
            else:
                data = await self._call_action(action, params)
        except OneBotApiError as e:
            if e.kind == OneBotErrorKind.LOCAL_ERROR:
                raise  # local-side condition: no capability marking; caller decides on retry
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
