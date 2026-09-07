"""Netdisk transfer handlers (bridge transfer, cancel, retry, tasks, index)."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.web import error_response, json_response
from core.api_validate import json_body, pick
from commands.handlers import Services

from .webapi_base import _ensure_ready


__all__ = [
    "api_bridge_transfer",
    "api_bridge_tasks",
    "api_bridge_cancel",
    "api_bridge_retry",
    "api_bridge_transfer_in",
    "api_netdisk_index",
]


async def api_bridge_transfer(s: Services) -> dict:
    """Archive group file to OpenList (bridge_out).

    Body:
        - group: group ID (required)
        - resource_ids: list of resource IDs (required)
        - force: force re-archive (optional, default false)
        - dst_dir: custom destination directory (optional)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    resource_ids = pick(payload, "resource_ids", cast=list, required=True)
    force = pick(payload, "force", cast=bool, default=False)
    dst_dir = pick(payload, "dst_dir", default="")

    # Permission check 
    managed = s.config.get("managed_groups", [])
    if not await s.scan.is_page_managed(group, managed):
        return error_response("group not managed", status_code=403)

    # Submit tasks
    results = []
    errors = []
    for rid in resource_ids:
        try:
            rid_int = int(rid)
            task_id = await s.bridge.submit_out(
                group, rid_int, dst_dir=dst_dir or None, force=force
            )
            results.append({"resource_id": rid_int, "task_id": task_id})
        except Exception as e:
            logger.warning(f"[webapi] submit_out resource_id={rid}: {e}", exc_info=True)
            errors.append(f"resource_id={rid}: transfer failed")

    return json_response({"results": results, "errors": errors})


async def api_bridge_tasks(s: Services) -> dict:
    """Bridge task list query.

    Body (optional):
        - direction: "out" or "in" (default: both)
        - state: filter by state (e.g., "pending", "done")
    """
    await _ensure_ready(s)
    if not s.bridge:
        return json_response({"tasks": []})

    payload = await json_body()
    direction = pick(payload, "direction", default="")
    state_filter = pick(payload, "state", default="")

    # Query tasks from archive_map
    if direction:
        directions = [direction]
    else:
        directions = ["out", "in"]

    all_tasks = []
    for d in directions:
        rows = await s.store.list_archive_map(
            states=("pending", "running", "done", "failed"), direction=d
        )
        for row in rows:
            if state_filter and row.get("state") != state_filter:
                continue
            all_tasks.append(row)

    return json_response({"tasks": all_tasks})


async def api_bridge_cancel(s: Services) -> dict:
    """Cancel bridge task.

    Body:
        - task_id: task ID to cancel (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    task_id = pick(payload, "task_id", required=True, empty_allowed=False)

    ok = await s.bridge.cancel(task_id)
    return json_response({"ok": ok, "task_id": task_id})


async def api_bridge_retry(s: Services) -> dict:
    """Retry failed bridge task.

    Body:
        - task_id: task ID to retry (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    task_id = pick(payload, "task_id", required=True, empty_allowed=False)

    ok = await s.bridge.retry(task_id)
    return json_response({"ok": ok, "task_id": task_id})


async def api_bridge_transfer_in(s: Services) -> dict:
    """Transfer file from OpenList netdisk to QQ group (bridge_in).

    Body:
        - group: target group ID (required)
        - path: file path on OpenList (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    path = pick(payload, "path", required=True, empty_allowed=False)

    # Permission check (same managed-group criteria as the Page)
    if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
        return error_response("group not managed", status_code=403)

    try:
        task_id = await s.bridge.submit_in(path, group_id=group)
        return json_response(
            {
                "ok": True,
                "task_id": task_id,
                "path": path,
                "group": group,
            }
        )
    except Exception as e:
        logger.warning(f"[webapi] transfer failed: {e}", exc_info=True)
        return error_response("transfer failed", status_code=500)


async def api_netdisk_index(s: Services) -> dict:
    """Deep index: {path} -> {task_id}."""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", default="/")
    try:
        task_id = await s.netdisk.submit_index(path)
        return json_response({"task_id": task_id, "path": path})
    except Exception as e:
        logger.warning(f"[webapi] submit index failed: {e}", exc_info=True)
        return error_response("submit index failed", status_code=502)
