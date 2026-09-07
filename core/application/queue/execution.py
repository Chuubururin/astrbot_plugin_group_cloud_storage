"""Execution loop -- worker pool, rate-limit acquisition, and retry policy
(OpQueue responsibility slice).

State attributes (_q/_q_hi/_running/_recent/_bulk/_limiter/_max_retries/
_backoff_base, etc.) are created in OpQueue.__init__; retry semantics live in
_execute.
"""

from __future__ import annotations

import asyncio
import time
from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger

from .op import BULK_KINDS, Op, OpCancelError, OpPausedError


class ExecutionMixin:
    # ---------- Lifecycle ----------

    def _respawn_worker(self, task: asyncio.Task) -> None:
        """Auto-respawn a worker that terminated unexpectedly (crash or
        unhandled exception). Skip during shutdown (workers cancelled
        intentionally)."""
        if self._shutting_down:
            return
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(f"[queue] worker {task.get_name()} crashed: {exc}")
        name = task.get_name()
        try:
            self._workers.remove(task)
        except ValueError:
            pass
        if "hi" in name:
            new = asyncio.create_task(self._worker_loop_hi(), name="op-queue-hi")
        else:
            new = asyncio.create_task(self._worker_loop(), name="op-queue")
        self._workers.append(new)
        new.add_done_callback(self._respawn_worker)

    async def start(self) -> None:
        self._shutting_down = False
        # Worker pool: high-priority and normal workers consume concurrently
        # across accounts
        hi = (self._slots + 1) // 2
        normal = self._slots - hi
        self._workers = [t for t in self._workers if not t.done()]
        for _ in range(
            hi - len([t for t in self._workers if t.get_name() == "op-queue-hi"])
        ):
            t = asyncio.create_task(self._worker_loop_hi(), name="op-queue-hi")
            self._workers.append(t)
            t.add_done_callback(self._respawn_worker)
        for _ in range(
            normal - len([t for t in self._workers if t.get_name() == "op-queue"])
        ):
            t = asyncio.create_task(self._worker_loop(), name="op-queue")
            self._workers.append(t)
            t.add_done_callback(self._respawn_worker)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers = []

    async def acquire(self, mult: float = 1.0, account=None) -> None:
        """Lets composite operations (scans, etc.) reuse rate limiting within
        a single Op; keyed by account (cross-account concurrency).
        """
        await self._limiter.acquire(mult=mult, account=account)

    # ---------- Execution loop ----------

    async def _worker_loop_hi(self) -> None:
        while True:
            op = await self._q_hi.get()
            if op.task_id in self._paused:  # pause hold: wait for resume (ledger records paused)
                self._paused[op.task_id] = op
                await self._ledger_state(op, "paused")
                continue
            self._pending.discard(op.task_id)
            await self._execute(op, high=True)

    async def _worker_loop(self) -> None:
        while True:
            op = await self._q.get()
            if op.task_id in self._paused:  # pause hold: wait for resume (ledger records paused)
                self._paused[op.task_id] = op
                await self._ledger_state(op, "paused")
                continue
            self._pending.discard(op.task_id)
            await self._execute(op, high=False)

    async def _execute(self, op: Op, high: bool) -> None:
        """Execute a single op (shared by both worker pools; rate limiting is
        shared).
        """
        keep_index = False
        released = False
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
        bulk = op.kind in BULK_KINDS
        try:
            if bulk:
                # Bulk operations: concurrency-capped, not rate-limited (QQ
                # calls are rate-limited per account by the adapter)
                await self._bulk.acquire()
            else:
                await self._limiter.acquire(account=getattr(op, "account", None))
            if op.cancel:  # cancelled while waiting on the rate limiter -> skip
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
            # Cooperative pause: hold until resumed (on resume the handler is
            # re-entered from the start, i.e. the task runs again)
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
            self._paused[op.task_id] = op  # preserves retry count/error; re-entered on resume
            keep_index = True
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Retriable: non-OneBot exceptions (e.g. transient library errors)
            # and transient/remote OneBot errors back off exponentially
            # (1s/2s/4s...); LOCAL_ERROR (e.g. missing bot context) is an
            # environment condition and fails immediately to avoid pointless
            # retries
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
                # Release resources BEFORE backoff sleep so other workers
                # can acquire them during the cooldown period
                if bulk:
                    self._bulk.release()
                released = True
                await asyncio.sleep(backoff)
                if high:
                    await self._q_hi.put(op)  # retry keeps its priority (high-priority queue)
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
            if bulk and not released:
                self._bulk.release()
            self._running.pop(op.task_id, None)
            self._cancelled.discard(op.task_id)
            if not keep_index:
                self._ops_by_id.pop(op.task_id, None)

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


__all__ = ["ExecutionMixin"]
