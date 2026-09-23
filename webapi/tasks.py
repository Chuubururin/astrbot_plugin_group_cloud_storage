"""Domain: Task management — handlers extracted from webapi.py."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body, pick
from .webapi_base import _ensure_ready


async def api_tasks(s: Services) -> dict:
    """Task record query (tasks tab): state/kind/target filters plus pagination.
    task_ids narrows to exact ids (relay chains poll their own tasks without
    the pagination window hiding them)."""
    payload = await json_body()
    state = pick(payload, "state", default=None) if payload else None
    kind = pick(payload, "kind", default=None) if payload else None
    target = pick(payload, "target", default=None) if payload else None
    limit = int(pick(payload, "limit", default=100) or 100) if payload else 100
    offset = int(pick(payload, "offset", default=0) or 0) if payload else 0
    task_ids = pick(payload, "task_ids", default=None) if payload else None
    if isinstance(task_ids, str):
        task_ids = [task_ids]
    elif isinstance(task_ids, list):
        task_ids = [str(t) for t in task_ids if t]
    else:
        task_ids = None
    tasks = await s.task_control.list_tasks(
        state=state, kind=kind, target=target, limit=limit, offset=offset,
        task_ids=task_ids,
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

    Finds tasks in op_ledger with state=pending and kind in the whitelist
    (convert_volumes, video_upload, netdisk_index), then re-enqueues each for
    recovery. The filter is applied in SQL: the ledger routinely holds
    hundreds of pending file_scan rows (one scan per hot-reload), and paging
    through them just to drop them was pure waste.

    Recovery goes through queue.claim(), not queue.submit(): submit mints a new
    task_id and the ledger upserts on task_id, so the row being resumed would
    stay pending while a second row tracked the work.

    Resumability pre-check: a stale "pending" row can reference inputs that no
    longer exist (converted resource, deleted staged file, moved netdisk path).
    Such a row is failed here instead of re-submitted -- the run would crash
    before any terminal write, and undo would report "interrupted" while nothing
    is running.
    """
    await _ensure_ready(s)
    # Single source: the same tuple ledger_reconcile uses to decide which kinds
    # a restart leaves resumable (adapters/persistence/sqlite/outbox.py).
    from adapters.persistence.sqlite.outbox import LEDGER_BREAKPOINT_KINDS

    kinds = sorted(LEDGER_BREAKPOINT_KINDS)
    breakpoint_rows: list[dict] = []
    offset = 0
    while True:
        page = await s.task_control.list_tasks(
            state="pending", kinds=kinds, limit=100, offset=offset
        )
        if not page:
            break
        breakpoint_rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
    if not breakpoint_rows:
        return json_response({"resumed": 0, "already_queued": 0,
                              "note": "无待恢复任务"})
    resumed = 0
    already_queued = 0
    failed_preflight = 0
    for row in breakpoint_rows:
        kind = row.get("kind", "")
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
        precheck_error = await _resume_precheck(s, kind, target, payload)
        if precheck_error:
            failed_preflight += 1
            logger.warning(
                f"[tasks-resume] {task_id} ({kind}) preflight failed: {precheck_error}"
            )
            await s.task_control.on_state(
                task_id, kind, target, payload, "failed", precheck_error
            )
            continue
        try:
            if await s.queue.claim(task_id, kind, target=target, payload=payload) is None:
                # Already queued or running in this process: counting it as
                # resumed would tell the user work was submitted that was not.
                already_queued += 1
            else:
                resumed += 1
        except Exception as e:
            logger.warning(f"[tasks-resume] claim {task_id} ({kind}) failed: {e}")
    out = {
        "resumed": resumed,
        "already_queued": already_queued,
        "failed_preflight": failed_preflight,
    }
    if not (resumed or already_queued or failed_preflight):
        # Every claim raised: without a note the frontend falls back to
        # "无待恢复任务" while the rows are still there, just not adoptable.
        out["note"] = f"{len(breakpoint_rows)} 个断点行认领失败，详见服务端日志"
    return json_response(out)


async def _resume_precheck(
    s: Services, kind: str, target: str, payload: dict
) -> str | None:
    """Validate that a breakpoint task's inputs still exist before
    re-submitting. Returns an error string, or None when resumable."""
    if kind == "convert_volumes":
        from core.application.composition.spec import is_composite

        rid = payload.get("resource_id") or ""
        # Legacy rows carry "group:file:<numeric-id>" (pre-Bug-16 rekeying);
        # newer ones key by the cloud file id ("group:file:<uuid>").
        detail = None
        if rid:
            detail = await s.store.get_resource_by_resource_id(rid)
        if detail is None:
            tail = str(rid).rsplit(":", 1)[-1] if rid else ""
            numeric: int | None = None
            if tail.isdigit():
                numeric = int(tail)
            elif isinstance(payload.get("id"), int):
                numeric = payload["id"]
            if numeric is not None:
                detail = (
                    await s.store.get_resource_detail(target, numeric)
                    or await s.store.get_resource_any(numeric)
                )
        if detail is None:
            return f"资源不存在（resource_id={rid or payload.get('id')}）"
        meta = detail.get("meta") or {}
        if is_composite(meta if isinstance(meta, dict) else None):
            return (
                f"资源 {detail.get('name') or detail.get('id')} "
                "已是分卷/组合形态"
            )
    elif kind == "video_upload":
        import os

        path = payload.get("path") or ""
        if not path or not os.path.isfile(path):
            return f"暂存文件不存在: {path or '(空)'}"
    elif kind == "netdisk_index":
        if not payload.get("path"):
            return "payload 缺少 path"
    return None
