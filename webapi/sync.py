"""Domain: Sync/withering handlers — handlers extracted from webapi.py."""

from __future__ import annotations

from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body
from .webapi_base import _ensure_ready


async def api_sync_withering(s: Services) -> dict:
    """Manually trigger the withering diff reconciliation.

    Body: {group_ids?: [...]} (empty = all managed groups)
    Enqueues a diff_file_scan task and returns task_id.
    """
    await _ensure_ready(s)
    try:
        payload = await json_body()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ids = (payload or {}).get("group_ids")
    managed = s.config.get("managed_groups", [])
    if isinstance(ids, list) and ids:
        valid = []
        for gid in ids:
            g = str(gid)
            # Withering is a data reconciliation path: managed check only
            # (offline accounts' groups are exactly its reconciliation target).
            if g and await s.scan.is_page_managed(g, managed):
                valid.append(g)
        if not valid:
            return error_response("no valid managed groups", status_code=400)
        task_id = await s.queue.submit(
            "diff_file_scan", target="*", payload={"mode": "diff", "groups": valid}
        )
        return json_response({"task_id": task_id, "groups": len(valid)})
    task_id = await s.queue.submit("diff_file_scan", target="*", payload={"mode": "diff"})
    return json_response({"task_id": task_id, "mode": "all"})


async def api_sync_status(s: Services) -> dict:
    """Scheduling status for the withering diff reconciliation.

    Returns {auto_scan_hours, last_diff_scan, running_diff_scans, queue_status}.
    """
    await _ensure_ready(s)
    hours = float(s.config.get("auto_scan_interval_hours", 6) or 0)
    # Find the most recent diff_file_scan task
    recent = await s.task_control.list_tasks(kind="diff_file_scan", limit=1)
    last_scan = None
    if recent:
        r = recent[0]
        last_scan = {
            "task_id": r.get("task_id"),
            "state": r.get("state"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "error": r.get("error"),
        }
    # Count diff_file_scan tasks currently running
    running = await s.task_control.list_tasks(kind="diff_file_scan", state="running", limit=100)
    queue_status = await s.task_control.queue_status()
    return json_response({
        "auto_scan_hours": hours,
        "auto_scan_enabled": hours > 0,
        "last_diff_scan": last_scan,
        "running_count": len(running),
        "queue": queue_status,
    })
