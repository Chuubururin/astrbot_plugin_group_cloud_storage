"""Database administration Page APIs for the embedded SQLite store."""
from __future__ import annotations
from astrbot.api.web import json_response, error_response, request
from core.api_validate import json_body
from .webapi_base import _ensure_ready

async def _admin(s, payload=None):
    await _ensure_ready(s)
    admin = getattr(s, "database_admin", None)
    if admin is None:
        raise RuntimeError("database administration unavailable")
    payload = payload or {}
    provided = payload.get("token")
    try:
        provided = provided or request.headers.get("X-Database-Admin-Token")
    except Exception:
        pass
    if not admin.authorize(provided):
        raise PermissionError("database administration unauthorized")
    return admin

async def api_database_health(s):
    return json_response(await (await _admin(s, {})).health())

async def api_database_integrity(s):
    return json_response(await (await _admin(s, {})).integrity())

async def api_database_backups(s):
    return json_response({"backups": await (await _admin(s, {})).backups()})

async def api_database_restore(s):
    payload = await json_body() or {}
    source = payload.get("source")
    if not source:
        return error_response("source required", status_code=400)
    return json_response(await (await _admin(s, payload)).restore(source))

async def api_database_backup(s):
    payload = await json_body() or {}
    return json_response(await (await _admin(s, payload)).backup(payload.get("destination")))
