"""Domain: Task management — handlers extracted from webapi.py."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body, pick
from .webapi_base import _ensure_ready


async def api_tasks(s: Services) -> dict:
    """Task record query (tasks tab): state/kind/target filters plus pagination."""
    payload = await json_body()
    state = pick(payload, "state", default=None) if payload else None
    kind = pick(payload, "kind", default=None) if payload else None
    target = pick(payload, "target", default=None) if payload else None
    limit = int(pick(payload, "limit", default=100) or 100) if payload else 100
    offset = int(pick(payload, "offset", default=0) or 0) if payload else 0
    tasks = await s.task_control.list_tasks(
        state=state, kind=kind, target=target, limit=limit, offset=offset
    )
    return json_response({"tasks": tasks, "total": len(tasks), "limit": limit, "offset": offset})


async def api_tasks_queue(s: Services) -> dict:
    """OpQueue live status (depth / running / paused-pending / recent records)."""
    return json_response(await s.task_control.queue_status())


async def api_tasks_pause(s: Services) -> dict:
    """Pause a task (queued = suspend; running = cooperative, takes effect at
    the next checkpoint)."""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.pause(task_id))


async def api_tasks_resume(s: Services) -> dict:
    """Resume a paused task."""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.resume(task_id))


async def api_tasks_interrupt(s: Services) -> dict:
    """Interrupt a task: queued = remove directly; running = cooperative
    cancel at the checkpoint; paused = set to a terminal state."""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.interrupt(task_id))


async def api_tasks_undo(s: Services) -> dict:
    """Undo a task:
    - {task_id}: not yet executed = discard; completed = compensate per the
      reversibility matrix (reverse a move, restore a rename)
    - {group_id, id}: snapshot restore for direct file-tag operations
    - Delete operations are irreversible in the cloud and are reported as
      "not undoable"
    """
    payload = await json_body()
    task_id = (payload or {}).get("task_id")
    group_id = (payload or {}).get("group_id")
    rid = (payload or {}).get("id")
    return json_response(
        await s.task_control.undo(
            task_id=task_id, group_id=group_id,
            resource_id=int(rid) if isinstance(rid, int) else None,
        )
    )


async def api_tasks_ops(s: Services) -> dict:
    """Operation log records (located by task_id or a direct {group_id, id}
    operation)."""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if task_id:
        ops = await s.task_control.ops(task_id)
    else:
        group_id = (payload or {}).get("group_id")
        rid = (payload or {}).get("id")
        if not isinstance(rid, int) or not group_id:
            return error_response("task_id 或 (group_id, id) required", status_code=400)
        op = await s.store.ops_last_for_resource("tags", rid)
        ops = [op] if op is not None else []
    return json_response({"task_id": task_id, "ops": ops})


async def api_tasks_resume_pending(s: Services) -> dict:
    """Re-submit breakpoint-resumable pending tasks (whitelist-based).

    Finds tasks in op_ledger with state=pending and kind in the whitelist,
    then re-enqueues each for recovery. Whitelist: convert_volumes,
    video_upload, netdisk_index (kept in sync with ledger_reconcile).
    """
    await _ensure_ready(s)
    pending = await s.task_control.list_tasks(state="pending", limit=200)
    if not pending:
        return json_response({"resumed": 0, "note": "无待恢复任务"})
    _BREAKPOINT_KINDS = {"convert_volumes", "video_upload", "netdisk_index"}
    resumed = 0
    skipped = 0
    for row in pending:
        kind = row.get("kind", "")
        if kind not in _BREAKPOINT_KINDS:
            skipped += 1
            continue
        task_id = row.get("task_id", "")
        target = row.get("target", "")
        payload = {}
        try:
            import json as _json
            raw = row.get("payload")
            if isinstance(raw, str):
                payload = _json.loads(raw)
            elif isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
        try:
            await s.queue.submit(kind, target=target, payload=payload)
            resumed += 1
            # Mark as running (prevents duplicate re-submission)
            await s.task_control.on_state(task_id, kind, target, payload, "running")
        except Exception as e:
            logger.warning(f"[tasks-resume] re-submit {task_id} ({kind}) failed: {e}")
    return json_response({
        "resumed": resumed,
        "skipped": skipped,
        "total_pending": len(pending),
    })
