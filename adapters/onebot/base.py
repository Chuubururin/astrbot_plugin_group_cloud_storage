"""NapCatBase -- NapCat adapter foundation: call channel, rate limiting,
and capability probing."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from adapters.limiter.interval import IntervalLimiter
from adapters.limiter.tier import interval_mult
from core.domain.enums import CapabilityState, OneBotApiError, OneBotErrorKind
from core.log import logger

# Exception message hints treated as "unsupported".
#
# Action-level phrasing ONLY. Verified against the live protocol bundle
# (SnowLuma/NapCat, /app/runtime/config-DwoxthVc.js): the WS dispatcher answers
# an unknown action with `retcode=1404, wording="unknown action"`. Resource-level
# misses use entirely different wordings that all contain "not found" ("message
# not found", "image not found in cache", "record not found in cache", "stream
# not found"), and the same protocol emits "unsupported" for ~20 unrelated
# conditions (content-type, media format, message element type). Matching those
# here marks the *action* UNSUPPORTED for the whole process lifetime: `_states`
# is never reset, and both consumers treat a cached UNSUPPORTED as final (the
# album upload gate in core/application/ingest/album.py, and the bridge
# URL-upload probe in core/application/bridge/inbound.py). A false UNSUPPORTED
# silently disables a working action forever; a false REMOTE_ERROR only costs
# three bounded retries.
_UNSUPPORTED_HINTS = (
    "unknown action",
    "no such action",
    "action not found",
    "action not exist",
    "unsupported action",
    "api not found",
    "api not exist",
    "unsupported api",
    "method not exist",
    "无此接口",
    "接口不存在",
    "api不存在",
    "不支持的api",
)

# Protocol-defined "unknown action" retcode (SnowLuma/NapCat RETCODE map:
# ACTION_FAILED=100, BAD_REQUEST=1400, UNKNOWN_ACTION=1404). Only 1404 means "this
# action does not exist"; 100/1400 are resource or argument failures and must not
# disable the action. This replaces the old bare "404" hint, which matched this
# code by accident ("1404" contains "404") and also matched forwarded HTTP 404s
# from a dead download URL.
_UNKNOWN_ACTION_RETCODE = 1404


class NapCatBase:
    def __init__(
        self,
        call_action: Callable[..., Awaitable[Any]],
        interval: float = 0.5,
    ):
        """Initialize the adapter base.

        Args:
            call_action: async (action, **params) -> data, bound to the AstrBot
                OneBot event; typical implementation:
                lambda action, **p: bot.call_action(action, **p)
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
                # BUG-20 fix: unpack params as keyword arguments to match the
                # _account_bot path. The constructor doc shows _call_action
                # should accept (action, **params), not (action, dict).
                data = await self._call_action(action, **params)
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
        # ActionFailed.retcode is a *property* (aiocqhttp: return
        # self.result['retcode']), so a response body without that key raises
        # KeyError -- and getattr() only swallows AttributeError. The raw
        # KeyError used to escape _classify and reach the caller as a
        # non-OneBotApiError, which OpQueue treats as retryable instead of a
        # capability verdict. Same semantics as before: 1404 -> UNSUPPORTED.
        try:
            retcode = getattr(src, "retcode", None)
        except Exception:
            retcode = None
        if (
            retcode == _UNKNOWN_ACTION_RETCODE
            or any(h in msg.lower() for h in _UNSUPPORTED_HINTS)
        ):
            self._mark(action, CapabilityState.UNSUPPORTED)
            raise OneBotApiError(OneBotErrorKind.UNSUPPORTED, action, msg) from src
        if "timeout" in msg.lower():
            raise OneBotApiError(OneBotErrorKind.TIMEOUT, action, msg) from src
        raise OneBotApiError(OneBotErrorKind.REMOTE_ERROR, action, msg) from src

    async def close(self) -> None:
        pass
