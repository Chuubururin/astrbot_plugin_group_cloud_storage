"""TaskControlService —— 任务台账与控制（v15；ADR-0005 经纠偏 D-6 直接指令实施）。

- 台账挂钩：OpQueue 状态机全部落库（op_ledger），可逆操作流落库（op_ops）
- 控制：暂停 / 继续 / 中断（协作式，见 OpQueue.pause_check / pause_task / resume_task /
  interrupt_task）；定时扫描类任务（file_scan 等）同受管控
- 撤销（按可逆性矩阵，D-6）：
  - 排队未执行 → 撤销即丢弃（中断语义）
  - 已完成 → 移动→反向移动；改名重传→原状恢复；标签（直连）→快照恢复
  - 删除类操作云端不可逆 → 明示「不可撤销」，不做伪撤销
- 启动对账：宿主重启后白名单 kind（转分卷/长视频/网盘索引）置 pending（断点续传候选），
  其余置 failed（ADR-0005 语义）
"""

from __future__ import annotations

from core.log import logger

# 可逆操作（经操作流补偿撤销）
_REVERSIBLE_KINDS = {"move_file", "replace_name"}


class TaskControlService:
    def __init__(self, store, queue, ops=None):
        self.store = store
        self.queue = queue
        self.file_ops = ops  # 补偿执行（撤销需要）；台账挂钩不需要

    # ---------- 台账挂钩（OpQueue 注入） ----------

    async def on_state(
        self, task_id: str, kind: str, target: str, payload: dict,
        state: str, error: str | None = None,
    ) -> None:
        try:
            await self.store.ledger_upsert(
                task_id, kind, target, payload, state, error=error
            )
        except Exception as e:  # 台账写入失败不阻断队列调度
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
        """启动对账（ADR-0005）：返回受影响行数。"""
        return await self.store.ledger_reconcile()

    # ---------- 查询 ----------

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

    # ---------- 控制（暂停/继续/中断） ----------

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
        hit = await self.queue.interrupt_task(task_id)
        if not hit:
            return {"ok": False, "task_id": task_id, "reason": "task not found or terminal"}
        return {"ok": True, "task_id": task_id, "action": "interrupted"}

    # ---------- 撤销（D-6 可逆性矩阵） ----------

    async def undo(
        self,
        task_id: str | None = None,
        group_id: str | None = None,
        resource_id: int | None = None,
    ) -> dict:
        """撤销：未执行=丢弃；已完成=矩阵补偿；删除类=明示不可撤销。"""
        if resource_id is not None and group_id is not None:
            return await self._undo_direct_tags(group_id, resource_id)
        if not task_id:
            return {"ok": False, "reason": "task_id 或 (group_id, resource_id) 必须提供其一"}
        row = await self.store.ledger_get(task_id)
        if row is None:
            return {"ok": False, "task_id": task_id, "reason": "task not found"}
        state = row["state"]
        if state in ("pending", "paused"):
            hit = await self.queue.interrupt_task(task_id)
            return {"ok": hit, "task_id": task_id, "action": "discard",
                    "note": "未执行，撤销即丢弃"}
        if state in ("failed", "cancelled"):
            return {"ok": True, "task_id": task_id, "action": "discard",
                    "note": "已终态（失败/取消），无需补偿"}
        if state == "done":
            return await self._compensate(task_id, row)
        # running/retry：撤销 = 协作式中断（无补偿，运行现场不留）
        await self.queue.interrupt_task(task_id)
        return {"ok": True, "task_id": task_id, "action": "interrupted",
                "note": "运行中任务已中断（不做补偿）"}

    async def _undo_direct_tags(self, group_id: str, resource_id: int) -> dict:
        """标签直连操作撤销：按资源定位最近一次快照并恢复（可反复翻转）。"""
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
            # 区分可逆类型但无记录 vs 不可逆类型
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