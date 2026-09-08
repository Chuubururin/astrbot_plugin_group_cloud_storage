"""Read-only netdisk/bridge query handlers (status, browse, link, meta, archived)."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from core.api_validate import json_body, pick
from commands.handlers import Services

from .webapi_base import _ensure_ready, _param


__all__ = [
    "api_bridge_status",
    "api_bridge_config_get",
    "api_bridge_task",
    "api_bridge_netdisk",
    "api_bridge_archived",
    "api_netdisk_meta",
    "api_netdisk_link",
]


async def api_bridge_status(s: Services) -> dict:
    """Bridge status and capability check.

    Returns:
        - enabled: whether bridge is configured
        - capability: OpenList client capability (UNKNOWN/OK/BROKEN)
        - dlserver_ready: whether download server is ready 
        - pending_out/in: pending task counts
    """
    await _ensure_ready(s)
    if not s.bridge:
        return json_response(
            {
                "enabled": False,
                "capability": "disabled",
                "dlserver_ready": False,
                "pending_out": 0,
                "pending_in": 0,
            }
        )

    status = await s.bridge.status()
    return json_response(status)


async def api_bridge_config_get(s: Services) -> dict:
    """Get OpenList bridge configuration.

    Returns current OpenList config (password masked).
    """
    await _ensure_ready(s)
    cfg = s.config
    return json_response(
        {
            "openlist_enabled": cfg.get("openlist_enabled", False),
            "openlist_base_url": cfg.get("openlist_base_url", ""),
            "openlist_username": cfg.get("openlist_username", ""),
            "openlist_password": "***" if cfg.get("openlist_password") else "",
            "openlist_token": "***" if cfg.get("openlist_token") else "",
            "openlist_dst_dir": cfg.get("openlist_dst_dir", "/"),
            "openlist_dst_dir_template": cfg.get(
                "openlist_dst_dir_template", "{group_id}/{filename}"
            ),
            "openlist_timeout_sec": cfg.get("openlist_timeout_sec", 30),
            "openlist_allow_private_address": cfg.get(
                "openlist_allow_private_address", False
            ),
            "openlist_poll_interval_sec": cfg.get("openlist_poll_interval_sec", 0),
            "bridge_min_size": cfg.get("bridge_min_size", "0"),
            "bridge_max_size": cfg.get("bridge_max_size", "0"),
            "download_server_enabled": cfg.get("download_server_enabled", False),
            "download_server_host": cfg.get("download_server_host", "127.0.0.1"),
            "download_http_port": cfg.get("download_http_port", 6186),
        }
    )


async def api_bridge_task(s: Services) -> dict:
    """Query a single bridge task by task_id (GET bridge/task?task_id=)."""
    task_id = await _param("task_id", "")
    if not task_id:
        return error_response("task_id is required", status_code=400)
    if not s.bridge:
        return json_response({"task_id": task_id, "state": "unknown", "enabled": False})
    return json_response(await s.bridge.status(task_id))


async def api_bridge_netdisk(s: Services) -> dict:
    """Browse OpenList netdisk directory.

    Body:
        - path: directory path (default: "/")
    """
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)

    payload = await json_body()
    path = pick(payload, "path", default="/")
    page = pick(payload, "page", cast=int, default=1)
    page_size = pick(payload, "page_size", cast=int, default=50)

    try:
        data = await s.netdisk.browse(path, max(1, page), max(1, min(page_size, 500)))
        return json_response(data)
    except Exception as e:
        logger.warning(f"[webapi] list dir failed: {e}", exc_info=True)
        return error_response("list dir failed", status_code=502)


async def api_bridge_archived(s: Services) -> dict:
    """Get archived resource IDs for a group (for badge display).

    Query params:
        - group: group ID (required)
    """
    await _ensure_ready(s)
    group = request.query.get("group", "")
    if not group:
        return error_response("group required", status_code=400)

    # Query all done archive entries for this group
    rows = await s.store.list_archive_map(states=("done",), direction="out")
    # Filter by group
    archived_ids = [
        r["resource_id"]
        for r in rows
        if r.get("group_id") == group and r.get("resource_id", 0) > 0
    ]
    return json_response({"group": group, "archived_ids": archived_ids})


async def api_netdisk_meta(s: Services) -> dict:
    """Set netdisk file tags: {path, tags: []}; replaces the existing tag set."""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)
    raw = pick(payload, "tags", cast=list, default=[])
    tags = [str(t).strip() for t in raw if str(t).strip()][:10]
    await s.netdisk.set_tags(path, tags)
    return json_response({"path": path, "tags": tags})


async def api_netdisk_link(s: Services) -> dict:
    """Get a direct link for a netdisk file."""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)
    try:
        return json_response({"url": await s.netdisk.direct_link(path)})
    except Exception as e:
        logger.warning(f"[webapi] get link failed: {e}", exc_info=True)
        return error_response("get link failed", status_code=502)
