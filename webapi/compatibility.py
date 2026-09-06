"""WebAPI compatibility seams shared by the registration facade and handlers.

This module is the single home for legacy request-helper monkeypatching, lazy
service binding, and route capture.  The public names remain available from
``webapi.webapi`` and ``webapi.webapi_base`` for older integrations.
"""
from __future__ import annotations

import functools
from astrbot.api.web import error_response
from commands.handlers import Services
from core.api_validate import ApiValidationError
from core.api_validate import json_body, pick, qi
PLUGIN_NAME = "astrbot_plugin_group_cloud_storage"

def bind_request_helper(module, facade_globals=None):
    """Refresh module helpers from the facade, preserving old patch seams."""
    source = facade_globals or globals()
    module.json_body = source.get("json_body", json_body)
    module.pick = source.get("pick", pick)
    module.qi = source.get("qi", qi)


def compat_handler(fn, module, facade_globals=None):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        bind_request_helper(module, facade_globals)
        result = await fn(*args, **kwargs)
        if isinstance(result, dict) and result.get("status_code", 200) >= 400:
            result.setdefault("status", "error")
            result.setdefault("message", result.get("error", "request failed"))
        return result
    return wrapped



class Bound:
    def __init__(self, s: Services, fn):
        self._s, self._fn = s, fn
    async def __call__(self, **kwargs):
        if self._s.ready is not None:
            await self._s.ready()
        try:
            return await self._fn(self._s, **kwargs)
        except ApiValidationError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            return error_response(str(exc), status_code=500)

__all__ = ["PLUGIN_NAME", "Bound", "compat_handler", "bind_request_helper"]
