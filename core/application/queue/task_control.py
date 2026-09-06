"""TaskControlService -- task ledger and control.

- Ledger hooks: every OpQueue state transition is persisted (op_ledger);
  reversible operation log entries are persisted (op_ops)
- Control: pause / resume / interrupt (cooperative; see OpQueue.pause_check /
  pause_task / resume_task / interrupt_task); scheduled scan tasks
  (file_scan etc.) are governed as well
- Undo (by reversibility matrix):
  - queued and not yet executed -> undo discards it (interrupt semantics)
  - done -> move reversed by a reverse move; rename re-upload restored to the
    original name; tags (direct) restored from the snapshot
  - delete operations are irreversible on the cloud side -> reported as not
    undoable, no fake undo
- Startup reconciliation: after a host restart, whitelisted kinds (volume
  conversion/long video/netdisk index) are set to pending (resume candidates),
  everything else to failed
"""

from __future__ import annotations

from core.log import logger

# Reversible operations (undone via operation log compensation)
_REVERSIBLE_KINDS = {"move_file", "replace_name"}


class TaskControlService:
    def __init__(self, store, queue, ops=None):
        self.store = store
        self.queue = queue
        self.file_ops = ops  # compensation executor (undo only); ledger hooks do not use it

    # ---------- Ledger hooks (injected into OpQueue) ----------

    async def on_state(
        self, task_id: str, kind: str, target: str, payload: dict,
        state: str, error: str | None = None,
    ) -> None:
        try:
            await self.store.ledger_upsert(
                task_id, kind, target, payload, state, error=error
            )
        except Exception as e:  # ledger write failures must not block queue scheduling
            logger.debug(f"[task-control] ledger write failed: {e}")

    async def on_op(
        self, task_id: str, action: str, before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        try:
            await self.store.ops_append(task_id, action, before, after)
        except Exception as e:
            logger.debug(f"[task-control] op record failed: {e}")

    async def reconcile(self) -> int:
        """Startup reconciliation: returns the number of affected rows."""
        return await self.store.ledger_reconcile()

    # ---------- Queries ----------

    async def list_tasks(
        self, state: str | None = None, kind: str | None = None,
        target: str | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        return await self.store.ledger_query(
            state=state, kind=kind, target=target, limit=limit, offset=offset
        )

    async def queue_status(self) -> dict:
        return await self.queue.status()

    async def ops(self, task_id: str) -> list[dict]:
        return await self.store.ops_list(task_id)

    # ---------- Control (pause/resume/interrupt) ----------

    async def pause(self, task_id: str) -> dict:
        r = self.queue.pause_task(task_id)
        if r == "unknown":
            return {"ok": False, "task_id": task_id, "reason": "task not found or terminal"}
        return {"ok": True, "task_id": task_id, "state": r,
                "note": "运行中任务为协作式暂停（下一检查点生效）" if r == "running" else ""}

    async def resume(self, task_id: str) -> dict:
        r = self.queue.resume_task(task_id)
        if r == "unknown":
            return {"ok": False, "task_id": task_id, "reason": "task not paused"}
        return {"ok": True, "task_id": task_id, "state": r}

    async def interrupt(self, task_id: str) -> dict:
        hit = self.queue.interrupt_task(task_id)
        if not hit:
            return {"ok": False, "task_id": task_id, "reason": "task not found or terminal"}
        return {"ok": True, "task_id": task_id, "action": "interrupted"}

    # ---------- Undo (reversibility matrix) ----------

    async def undo(
        self,
        task_id: str | None = None,
        group_id: str | None = None,
        resource_id: int | None = None,
    ) -> dict:
        """Undo: not executed = discard; done = matrix compensation;
        deletes = explicitly not undoable.
        """
        if resource_id is not None and group_id is not None:
            return await self._undo_direct_tags(group_id, resource_id)
        if not task_id:
            return {"ok": False, "reason": "task_id 或 (group_id, resource_id) 必须提供其一"}
        row = await self.store.ledger_get(task_id)
        if row is None:
            return {"ok": False, "task_id": task_id, "reason": "task not found"}
        state = row["state"]
        if state in ("pending", "paused"):
            hit = self.queue.interrupt_task(task_id)
            return {"ok": hit, "task_id": task_id, "action": "discard",
                    "note": "未执行，撤销即丢弃"}
        if state in ("failed", "cancelled"):
            return {"ok": True, "task_id": task_id, "action": "discard",
                    "note": "已终态（失败/取消），无需补偿"}
        if state == "done":
            return await self._compensate(task_id, row)
        # running/retry: undo = cooperative interrupt (no compensation, no
        # running state preserved)
        self.queue.interrupt_task(task_id)
        return {"ok": True, "task_id": task_id, "action": "interrupted",
                "note": "运行中任务已中断（不做补偿）"}

    async def _undo_direct_tags(self, group_id: str, resource_id: int) -> dict:
        """Undo a direct tags operation: locate the latest snapshot for the
        resource and restore it (can be toggled repeatedly).
        """
        op = await self.store.ops_last_for_resource("tags", resource_id)
        if op is None:
            return {"ok": False, "group_id": group_id, "id": resource_id,
                    "undoable": False, "reason": "无标签操作流记录"}
        new_tags = op["after"].get("tags") or []
        old_tags = op["before"].get("tags") or []
        await self.store.update_resource_tags(resource_id, old_tags)
        await self.store.ops_append(
            "",
            "tags",
            before={"group_id": group_id, "id": resource_id, "tags": new_tags},
            after={"group_id": group_id, "id": resource_id, "tags": old_tags},
        )
        return {"ok": True, "group_id": group_id, "id": resource_id,
                "action": "tags_restored"}

    async def _compensate(self, task_id: str, row: dict) -> dict:
        kind = row["kind"]
        ops = await self.store.ops_list(task_id)
        if not ops:
            # Distinguish a reversible kind with no log from an irreversible kind
            if kind in _REVERSIBLE_KINDS:
                return {"ok": False, "task_id": task_id, "undoable": False,
                        "reason": f"{kind} 无操作记录（可能在修复前执行，无法追溯原始参数）"}
            return {"ok": False, "task_id": task_id, "undoable": False,
                    "reason": f"{kind} 操作不可撤销"}
        last = ops[-1]
        before = last.get("before") or {}
        try:
            if kind == "move_file":
                if self.file_ops is None:
                    return {"ok": False, "task_id": task_id, "undoable": False,
                            "reason": "文件操作服务不可用"}
                rid = before.get("id")
                if rid is None:
                    return {"ok": False, "task_id": task_id, "undoable": False,
                            "reason": "操作记录缺少资源 ID"}
                gid = before.get("group_id") or row.get("target") or ""
                new_task = await self.file_ops.submit_move(
                    gid, rid, str(before.get("folder") or "!/")
                )
                return {"ok": True, "task_id": task_id, "action": "reverse_move",
                        "compensation_task_id": new_task,
                        "note": "已提交反向移动任务"}
            if kind == "replace_name":
                if self.file_ops is None:
                    return {"ok": False, "task_id": task_id, "undoable": False,
                            "reason": "文件操作服务不可用"}
                rid, old_name = before.get("id"), before.get("name")
                if not rid or not old_name:
                    return {"ok": False, "task_id": task_id, "undoable": False,
                            "reason": "操作记录缺少资源定位"}
                gid = before.get("group_id") or row.get("target") or ""
                new_task = await self.file_ops.submit_replace_name(gid, rid, str(old_name))
                return {"ok": True, "task_id": task_id, "action": "restore_name",
                        "compensation_task_id": new_task,
                        "note": "已提交恢复原名任务"}
        except ValueError as e:
            return {"ok": False, "task_id": task_id, "undoable": False, "reason": str(e)}
        if kind in ("delete",):
            return {"ok": False, "task_id": task_id, "undoable": False,
                    "reason": "删除操作云端不可逆（文件已从云端移除）"}
        return {"ok": False, "task_id": task_id, "undoable": False,
                "reason": f"{kind} 操作不支持撤销（仅 移动/改名/标签 可撤销）"}