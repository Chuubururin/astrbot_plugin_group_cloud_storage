"""WebAPI compatibility seams shared by the registration facade and handlers.

This module is the single home for legacy request-helper monkeypatching, lazy
service binding, and route capture.  The public names remain available from
``webapi.webapi`` and ``webapi.webapi_base`` for older integrations.
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.web import error_response
from commands.handlers import Services
from core.api_validate import ApiValidationError
from core.api_validate import json_body, pick, qi
from .routes import PLUGIN_NAME  # noqa: F401  (re-export; the single definition lives in routes.py)


def bind_request_helper(module: Any, facade_globals: dict[str, Any] | None = None) -> None:
    """Refresh module helpers from the facade, preserving old patch seams."""
    source = facade_globals or globals()
    module.json_body = source.get("json_body", json_body)
    module.pick = source.get("pick", pick)
    module.qi = source.get("qi", qi)


def compat_handler(
    fn: Callable[..., Awaitable[Any]],
    module: Any,
    facade_globals: dict[str, Any] | None = None,
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        bind_request_helper(module, facade_globals)
        result = await fn(*args, **kwargs)
        if isinstance(result, dict) and result.get("status_code", 200) >= 400:
            result.setdefault("status", "error")
            result.setdefault("message", result.get("error", "request failed"))
        return result
    return wrapped


class Bound:
    def __init__(self, s: Services, fn: Callable[..., Awaitable[Any]]) -> None:
        self._s = s
        self._fn = fn

    async def __call__(self, **kwargs: Any) -> dict:
        try:
            if self._s.ready is not None:
                await self._s.ready()
            return await self._fn(self._s, **kwargs)
        except ApiValidationError as exc:
            return error_response(str(exc), status_code=400)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except PermissionError as exc:
            return error_response(str(exc) or "forbidden", status_code=403)
        except FileNotFoundError as exc:
            return error_response(str(exc) or "not found", status_code=404)
        except Exception as exc:
            logger.warning(f"[webapi] unhandled: {exc}", exc_info=True)
            return error_response("internal server error", status_code=500)


def handle_api_error(e: Exception, *, label: str = "operation") -> dict:
    """Sanitize exception for user-facing error response.

    ValueError → 400 with message (user-facing validation).
    Other exceptions → 500 with generic message; details logged only.
    """
    if isinstance(e, ValueError):
        return error_response(str(e), status_code=400)
    logger.warning(f"[webapi] {label} failed: {e}", exc_info=True)
    return error_response(f"{label} failed", status_code=500)


__all__ = [
    "PLUGIN_NAME",
    "Bound",
    "compat_handler",
    "bind_request_helper",
    "handle_api_error",
]
