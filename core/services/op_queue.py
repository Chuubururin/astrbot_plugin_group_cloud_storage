"""OpQueue —— 统一限速操作队列（docs/09 §12.5）。

目标：Page 上的一切外部操作（扫描/改名/上传/删除/移动/同步）排队执行，
复用 IntervalLimiter（500ms 间隔 + 退避）对抗 QQ 风控；SSE 订阅者实时收到进度。

约定：
- 执行函数由使用方注入（run_handler），OpQueue 只负责调度与限速（单一职责）
- 重试：可重试异常（OneBotErrorKind.TIMEOUT/RATE_LIMITED/REMOTE_ERROR）指数退避
- 取消：op.cancel 置位后跳过该 op（docs/06 取消约定：终态写入由 handler 负责）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from adapters.limiter.interval import IntervalLimiter, KeyedLimiter
from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger


@dataclass
class Op:
    """一个待执行的外部操作。"""

    task_id: str
    kind: str  # scan | rename | upload | delete | move | sync
    target: str = ""  # group_id
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    retries: int = 0
    cancel: bool = False
    pause: bool = False  # v15：协作式暂停（handler 检查点生效）
    error: str | None = None
    account: str | None = None  # v2.11：账号键控（限速/并发作用域）


class OpCancelError(Exception):
    """操作被取消。"""


class OpPausedError(Exception):
    """操作被暂停（协作式：handler 在检查点抛出，worker 挂起待恢复）。"""


class OpQueue:
    def __init__(
        self,
        run_handler: Callable[[Op], Awaitable[None]],
        interval: float = 0.5,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        limiter: IntervalLimiter | None = None,
        high_priority: set[str] | None = None,
        slots: int = 4,
        ledger=None,  # v15：任务台账挂钩（on_state/on_op，见 TaskControlService）
    ):
        self._run_handler = run_handler
        # 优先级集合可配置（config.op_high_priority_kinds）；None = 内置默认
        self._high_priority = (
            high_priority if high_priority is not None else self._DEFAULT_HIGH_PRIORITY
        )
        # v2.11 键控限速：默认键=全局限速（兼容旧行为）；
        # 传入 limiter 时注入为默认键（与扫描复合操作共享同一节奏）
        self._limiter = KeyedLimiter(interval=interval)
        if limiter is not None:
            self._limiter.inject(self._limiter.default_key, limiter)
        # 重活分流：非交互重活（转分卷/视频处理/拉取/出库）不限速只限并发，
        # QQ 调用由适配器按账号键控限速（跨账号并发）
        self._bulk = asyncio.Semaphore(2)
        self._slots = max(2, int(slots))
        self._q_hi: asyncio.Queue = asyncio.Queue()
        self._q: asyncio.Queue = asyncio.Queue()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._listeners: set["asyncio.Queue[dict]"] = set()
        self._pending: set[str] = set()  # 已提交未执行（队列中或等待中）
        self._cancelled: set[str] = set()  # 被取消的任务（取出时检查）
        self._paused: dict[str, Op | None] = {}  # v15：暂停挂起（占位 None=仍在队列）
        self._workers: list[asyncio.Task] = []
        self._running: dict[str, Op] = {}
        self._recent: deque[dict] = deque(maxlen=20)
        self._lock = asyncio.Lock()
        self._ledger = ledger  # 任务台账（on_state/on_op）；None = 不落台账

    async def acquire(self, mult: float = 1.0, account=None) -> None:
        """供复合操作（扫描等）在单个 Op 内复用限速；account 键控（跨账号并发）。"""
        await self._limiter.acquire(key=account, mult=mult)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        # v2.11：多 worker 池（高优/常规各半），跨账号并发消费
        hi = (self._slots + 1) // 2
        normal = self._slots - hi
        self._workers = [t for t in self._workers if not t.done()]
        for i in range(
            hi - len([t for t in self._workers if t.get_name() == "op-queue-hi"])
        ):
            self._workers.append(
                asyncio.create_task(self._worker_loop_hi(), name="op-queue-hi")
            )
        for i in range(
            normal - len([t for t in self._workers if t.get_name() == "op-queue"])
        ):
            self._workers.append(
                asyncio.create_task(self._worker_loop(), name="op-queue")
            )

    async def shutdown(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers = []

    # ---------- 提交 ----------

    # v2.11：非交互重活（本地计算为主；API 子调用由适配器键控限速）
    _BULK_KINDS = {
        "convert_volumes",
        "video_upload",
        "video_album",
        "fetch",
        "netdisk_index",
    }

    _DEFAULT_HIGH_PRIORITY = {
        "rename",
        "move_file",
        "upload",
        "delete",
        "sync",
        "file_scan",
        "essence_save",
        "essence_delete",
        "fetch",
        "video_upload",
        "video_album",
        "convert_volumes",
        "batch_groups",
        "create_folder",
    }

    async def submit(
        self, kind: str, target: str = "", payload: dict | None = None, account=None
    ) -> str:
        await (
            self.start()
        )  # 幂等：worker 常驻，任何提交都保证被消费（Page 首调场景修复）
        op = Op(
            task_id=uuid.uuid4().hex[:12],
            kind=kind,
            target=target,
            payload=payload or {},
            account=account,
        )
        if kind in self._high_priority:
            await self._q_hi.put(op)
        else:
            await self._q.put(op)
        self._pending.add(op.task_id)
        await self._ledger_state(op, "pending")
        self._push(
            {
                "type": "queued",
                "task_id": op.task_id,
                "kind": op.kind,
                "target": op.target,
                "ts": time.time(),
            }
        )
        return op.task_id

    # ---------- 任务控制（v15，D-6：暂停/继续/中断/撤销） ----------

    def pause_task(self, task_id: str) -> str:
        """暂停任务（协作式）。

        Returns:
            "queued": 排队挂起
            "running": 运行中待检查点
            "paused": 已暂停
            "unknown": 任务不存在
        """
        if task_id in self._paused:
            return "paused"
        if task_id in self._pending:
            self._paused[task_id] = None  # 排队中：占位，worker 取出时挂起
            self._push(
                {
                    "type": "paused",
                    "task_id": task_id,
                    "ts": time.time(),
                }
            )
            return "queued"
        op = self._running.get(task_id)
        if op is not None:
            op.pause = True  # 运行中：handler 下一检查点生效（pause_check）
            return "running"
        return "unknown"

    def resume_task(self, task_id: str) -> str:
        """继续任务。

        Returns:
            "resumed": 已恢复
            "unknown": 任务不存在或未暂停
        """
        if task_id not in self._paused:
            return "unknown"
        entry = self._paused.pop(task_id)
        if entry is not None:  # 已取出挂起的真实 Op：按优先级重新入队
            entry.pause = False
            if entry.kind in self._high_priority:
                self._q_hi.put_nowait(entry)
            else:
                self._q.put_nowait(entry)
        self._push(
            {
                "type": "resumed",
                "task_id": task_id,
                "ts": time.time(),
            }
        )
        return "resumed"

    def cancel_task(self, task_id: str) -> bool:
        """取消任务：覆盖 队列中 / 限速等待中 / 暂停挂起 / 执行中 四种状态（执行中由 handler 协作）。"""
        hit = task_id in self._pending or task_id in self._running
        if task_id in self._paused:
            self._pending.discard(task_id)
            held = self._paused.pop(task_id, None)
            hit = True
            if held is not None:  # 挂起的真实 Op：直接置终态（不在队列中）
                self._push(
                    {
                        "type": "cancelled",
                        "task_id": held.task_id,
                        "kind": held.kind,
                        "target": held.target,
                        "ts": time.time(),
                    }
                )
                self._record(held, "cancelled")
                self._ledger_fire(held, "cancelled")
            # else: 仍在队列中，worker 取出时经 _execute 取消路径落终态
        self._cancelled.add(task_id)
        for op in list(self._running.values()):
            if op.task_id == task_id:
                op.cancel = True
                hit = True
        return hit

    async def interrupt_task(self, task_id: str) -> bool:
        """中断任务 = 取消（排队即移除；运行中协作式取消；暂停挂起置终态）。"""
        return self.cancel_task(task_id)

    async def pause_check(self, op: Op) -> None:
        """协作式检查点：handler 在 OneBot 调用间隔调用；
        任务被取消 → OpCancelError；被暂停 → OpPausedError。"""
        if op.cancel:
            raise OpCancelError()
        if op.pause:
            raise OpPausedError()

    async def record_op(
        self,
        task_id: str,
        action: str,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        """操作流记录（可逆操作的 before/after 快照；供撤销补偿）。"""
        if self._ledger is not None:
            await self._ledger.on_op(task_id, action, before, after)

    # 台账联动

    async def _ledger_state(
        self, op: Op, state: str, error: str | None = None
    ) -> None:
        if self._ledger is None:
            return
        await self._ledger.on_state(
            op.task_id, op.kind, op.target, op.payload, state, error
        )

    def _ledger_fire(self, op: Op, state: str, error: str | None = None) -> None:
        """同步上下文中异步落台账（取消/暂停路径的事件循环内调用）。"""
        if self._ledger is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(
            self._ledger.on_state(
                op.task_id, op.kind, op.target, op.payload, state, error
            )
        )

    # ---------- 执行循环 ----------

    async def _worker_loop_hi(self) -> None:
        while True:
            op = await self._q_hi.get()
            if op.task_id in self._paused:  # 暂停挂起：等到恢复（台账落 paused）
                self._paused[op.task_id] = op
                await self._ledger_state(op, "paused")
                continue
            self._pending.discard(op.task_id)
            await self._execute(op, high=True)

    async def _worker_loop(self) -> None:
        while True:
            op = await self._q.get()
            if op.task_id in self._paused:  # 暂停挂起：等到恢复（台账落 paused）
                self._paused[op.task_id] = op
                await self._ledger_state(op, "paused")
                continue
            self._pending.discard(op.task_id)
            await self._execute(op, high=False)

    async def _execute(self, op: Op, high: bool) -> None:
        """执行单个 op（常规/高优 worker 共用；限速共享防风控）。"""
        if op.task_id in self._cancelled:
            self._cancelled.discard(op.task_id)
            self._push(
                {
                    "type": "cancelled",
                    "task_id": op.task_id,
                    "kind": op.kind,
                    "target": op.target,
                    "ts": time.time(),
                }
            )
            self._record(op, "cancelled")
            await self._ledger_state(op, "cancelled")
            return
        if op.cancel:
            return
        self._running[op.task_id] = op
        bulk = op.kind in self._BULK_KINDS
        try:
            if bulk:
                # v2.11：重活分流——不限速只限并发（QQ 调用由适配器按账号限速）
                await self._bulk.acquire()
            else:
                await self._limiter.acquire(key=getattr(op, "account", None))
            if op.cancel:  # 限速等待期间被取消 → 跳过执行
                self._push(
                    {
                        "type": "cancelled",
                        "task_id": op.task_id,
                        "kind": op.kind,
                        "target": op.target,
                        "ts": time.time(),
                    }
                )
                self._record(op, "cancelled")
                await self._ledger_state(op, "cancelled")
                return
            self._push(
                {
                    "type": "started",
                    "task_id": op.task_id,
                    "kind": op.kind,
                    "target": op.target,
                    "ts": time.time(),
                }
            )
            await self._ledger_state(op, "running")
            await self._run_handler(op)
            self._push(
                {
                    "type": "done",
                    "task_id": op.task_id,
                    "kind": op.kind,
                    "target": op.target,
                    "ts": time.time(),
                }
            )
            self._record(op, "ok")
            await self._ledger_state(op, "done")
        except OpCancelError:
            self._push(
                {
                    "type": "cancelled",
                    "task_id": op.task_id,
                    "kind": op.kind,
                    "ts": time.time(),
                }
            )
            self._record(op, "cancelled")
            await self._ledger_state(op, "cancelled")
        except OpPausedError:
            # 协作式暂停：挂起待恢复（恢复后整体重入 handler，语义=任务重启执行）
            self._push(
                {
                    "type": "paused",
                    "task_id": op.task_id,
                    "kind": op.kind,
                    "ts": time.time(),
                }
            )
            self._record(op, "paused")
            await self._ledger_state(op, "paused")
            self._paused[op.task_id] = op  # 保留现场（重试次数/error），恢复时重入
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 可重试：仅瞬态/远端错误指数退避（1s/2s/4s...）；
            # LOCAL_ERROR（如缺少 bot 上下文）属环境态，直接失败避免无谓重试
            retriable = not isinstance(e, OneBotApiError) or (
                e.kind
                in (
                    OneBotErrorKind.TIMEOUT,
                    OneBotErrorKind.RATE_LIMITED,
                    OneBotErrorKind.REMOTE_ERROR,
                )
            )
            if retriable and op.retries < self._max_retries:
                op.retries += 1
                op.error = str(e)
                backoff = self._backoff_base**op.retries
                logger.warning(
                    f"[op-queue] {op.kind}/{op.task_id} failed ({e}), "
                    f"retry {op.retries}/{self._max_retries} after {backoff}s"
                )
                self._push(
                    {
                        "type": "retry",
                        "task_id": op.task_id,
                        "kind": op.kind,
                        "retries": op.retries,
                        "backoff": backoff,
                        "ts": time.time(),
                    }
                )
                await self._ledger_state(op, "retry", str(e))
                await asyncio.sleep(backoff)
                if high:
                    await self._q_hi.put(op)  # 重试保持优先级（进入高优队列）
                else:
                    await self._q.put(op)
            else:
                logger.error(
                    f"[op-queue] {op.kind}/{op.task_id} failed permanently: {e}"
                )
                self._push(
                    {
                        "type": "failed",
                        "task_id": op.task_id,
                        "kind": op.kind,
                        "error": str(e),
                        "ts": time.time(),
                    }
                )
                self._record(op, "failed", str(e))
                await self._ledger_state(op, "failed", str(e))
        finally:
            if bulk:
                self._bulk.release()
            self._running.pop(op.task_id, None)
            self._cancelled.discard(op.task_id)

    def _record(self, op: Op, state: str, error: str | None = None) -> None:
        self._recent.appendleft(
            {
                "task_id": op.task_id,
                "kind": op.kind,
                "target": op.target,
                "state": state,
                "error": error,
                "ts": time.time(),
            }
        )

    # ---------- SSE 订阅与状态 ----------

    def publish(self, ev: dict) -> None:
        """公开推送：供 op handler 内部分片进度（如扫描 i/N）使用。"""
        self._push({**ev, "ts": time.time()})

    def _push(self, ev: dict) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[dict]:
        """订阅事件流（消费方负责退出时 close）。"""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._listeners.add(q)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            self._listeners.discard(q)

    async def status(self) -> dict:
        return {
            "depth": self._q.qsize() + self._q_hi.qsize(),
            "high": self._q_hi.qsize(),
            "high_priority_kinds": sorted(self._high_priority),
            "running": [
                {
                    "task_id": o.task_id,
                    "kind": o.kind,
                    "target": o.target,
                    "retries": o.retries,
                }
                for o in self._running.values()
            ],
            "paused_ids": sorted(self._paused.keys()),
            "recent": list(self._recent),
            "slots": self._slots,
            "accounts": self._limiter.keys(),
        }
