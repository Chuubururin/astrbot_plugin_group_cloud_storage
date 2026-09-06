"""Thin WebAPI registration facade.

Handlers live in domain modules; route metadata in :mod:`webapi.routes`.
"""
from __future__ import annotations
import asyncio
import importlib
from astrbot.api.star import Context
# Facade namespace exports; referenced by tests and the compatibility layer.
from astrbot.api.web import json_response, error_response, request, stream_response  # noqa: F401
from astrbot.api import logger
from commands.handlers import Services
from core.api_validate import json_body
from .compatibility import PLUGIN_NAME, Bound as _Bound, compat_handler
from .routes import RouteRegistry, validate_routes
from . import resources as _resources
from . import config as _config_module

for _modname in ("groups", "resources", "tasks", "config", "sync", "albums", "events", "misc", "database", "webapi_ext", "webapi_netdisk", "netdisk_query", "netdisk_mutation", "netdisk_transfer"):
    _mod = importlib.import_module(f".{_modname}", __package__)
    for _name in dir(_mod):
        if _name.startswith("api_"):
            globals().setdefault(_name, getattr(_mod, _name))

for _modname, _names in (("tasks", ("api_tasks", "api_tasks_pause", "api_tasks_resume", "api_tasks_interrupt", "api_tasks_undo", "api_tasks_ops", "api_tasks_resume_pending")), ("sync", ("api_sync_withering", "api_sync_status"))):
    _mod = importlib.import_module(f".{_modname}", __package__)
    for _name in _names:
        globals()[_name] = compat_handler(globals()[_name], _mod, globals())

async def api_config_save(s):
    _config_module.json_body = json_body
    result = await _config_module.api_config_save(s)
    if isinstance(result, dict) and result.get("status_code", 200) >= 400:
        result.setdefault("status", "error")
    return result

def _aggregate_capacity(*args, **kwargs):
    return _resources._aggregate_capacity(*args, **kwargs)
GROUP_TOTAL_DEFAULT = _resources.GROUP_TOTAL_DEFAULT

# Plugin runtime context (injected by register_page_apis; hot reload reaches
# the framework star_manager through it)
_CONTEXT = None

async def api_config_reload(s):
    """Config hot reload: reload this plugin through the framework
    star_manager, matching the native plugin page behavior.

    The reload tears down and rebuilds the in-process plugin runtime, so it
    must run after the HTTP response is returned (frontend receives the
    reply and confirms, then the framework reloads and SSE reconnects).
    """
    from astrbot.api.star import Context as _Ctx  # noqa: F401
    sm = getattr(_CONTEXT, "_star_manager", None)
    if sm is None or not hasattr(sm, "reload"):
        return error_response("插件管理器不可用，请到 AstrBot 插件页手动重载", status_code=500)

    async def _delayed_reload():
        await asyncio.sleep(0.6)
        try:
            ok, msg = await sm.reload(PLUGIN_NAME)
            if ok:
                logger.info("[group_cloud_storage] config hot-reload done")
            else:
                logger.warning(f"[group_cloud_storage] config hot-reload failed: {msg}")
        except Exception as e:
            logger.warning(f"[group_cloud_storage] config hot-reload error: {e}")

    asyncio.get_running_loop().create_task(_delayed_reload(), name="config-hot-reload")
    return json_response({"status": "ok", "message": "reload scheduled"})

def register_page_apis(context: Context, s: Services) -> None:
    global _CONTEXT
    _CONTEXT = context
    validate_routes()
    RouteRegistry().register(context, PLUGIN_NAME, lambda name: _Bound(s, globals().get(name)))

__all__ = ["register_page_apis", "PLUGIN_NAME"]
