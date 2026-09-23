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
from core.task_cancel import cancel_tasks

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
            new = asyncio.create_task(
                self._worker_loop(self._q_hi, high=True), name="op-queue-hi"
            )
        else:
            new = asyncio.create_task(self._worker_loop(self._q), name="op-queue")
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
            t = asyncio.create_task(
                self._worker_loop(self._q_hi, high=True), name="op-queue-hi"
            )
            self._workers.append(t)
            t.add_done_callback(self._respawn_worker)
        for _ in range(
            normal - len([t for t in self._workers if t.get_name() == "op-queue"])
        ):
            t = asyncio.create_task(self._worker_loop(self._q), name="op-queue")
            self._workers.append(t)
            t.add_done_callback(self._respawn_worker)

    async def shutdown(self, timeout: float = 1.0) -> None:
        """Stop the worker pool -- bounded, and cheap in the common case.

        The bound itself lives in ``core.task_cancel.cancel_tasks`` (shared with
        ``RuntimeKernel.cancel_all``): deadline + ``asyncio.wait`` + re-cancel.
        Neither ``asyncio.sleep(timeout)`` (never early-exits) nor
        ``asyncio.wait_for`` (not a bound on CPython <= 3.11) is a real bound --
        see that module's docstring for the measurements and the CI-vs-local
        reproduction.
        """
        self._shutting_down = True
        workers = list(self._workers)
        self._workers = []
        if not workers:
            return
        await cancel_tasks(workers, timeout=timeout, label="queue")
        for w in workers:
            if w.done() and not w.cancelled():
                exc = w.exception()
                if exc is not None:
                    logger.warning(f"[queue] worker {w.get_name()} died: {exc}")

    async def acquire(self, mult: float = 1.0, account=None) -> None:
        """Lets composite operations (scans, etc.) reuse rate limiting within
        a single Op; keyed by account (cross-account concurrency).
        """
        await self._limiter.acquire(mult=mult, account=account)

    # ---------- Execution loop ----------

    async def _worker_loop(self, q: asyncio.Queue, high: bool = False) -> None:
        """Single consumption loop shared by both pools (the former
        _worker_loop_hi duplicate differed only in queue + priority). Task
        names stay "op-queue-hi"/"op-queue" for _respawn_worker/start()."""
        while True:
            op = await q.get()
            if self._shutting_down:
                q.task_done()
                return
            if op.task_id in self._paused:  # pause hold: wait for resume (ledger records paused)
                if op.cancel or op.task_id in self._cancelled:
                    # Interrupted between pause_task() and this dequeue: the
                    # queued-pause path relies on the worker to finalize the
                    # ledger; _execute is skipped here, so write "cancelled"
                    # now (re-writing "paused" would resurrect a dead task).
                    self._paused.pop(op.task_id, None)
                    self._cancelled.discard(op.task_id)
                    self._ops_by_id.pop(op.task_id, None)
                    await self._transition(
                        op, "cancelled", record="cancelled", ledger="cancelled"
                    )
                    continue
                self._paused[op.task_id] = op
                # Ledger written BEFORE the broadcast, and this paused event
                # carries no target and no _recent record -- unlike the
                # OpPausedError transition in _execute (kept as-is).
                await self._ledger_state(op, "paused")
                self._push({"type": "paused", "task_id": op.task_id, "kind": op.kind, "ts": time.time()})
                continue
            self._pending.discard(op.task_id)
            await self._execute(op, high=high)

    async def _transition(
        self,
        op: Op,
        event_type: str,
        *,
        target: bool = True,
        record: str | None = None,
        ledger: str | None = None,
        error: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """One state transition: SSE push + optional _recent record + optional
        ledger write, in that order. ``target=False`` matches the historical
        no-target event shapes (OpCancelError/OpPausedError/retry/failed);
        per-branch extras go through ``extra`` so payloads stay byte-identical.
        """
        event = {"type": event_type, "task_id": op.task_id, "kind": op.kind}
        if target:
            event["target"] = op.target
        if extra:
            event.update(extra)
        event["ts"] = time.time()
        self._push(event)
        if record is not None:
            self._record(op, record, error)
        if ledger is not None:
            await self._ledger_state(op, ledger, error)

    async def _execute(self, op: Op, high: bool) -> None:
        """Execute a single op (shared by both worker pools; rate limiting is
        shared).
        """
        keep_index = False
        released = False
        if op.task_id in self._cancelled:
            self._cancelled.discard(op.task_id)
            await self._finalize_cancelled(op)
            return
        if op.cancel:
            # H2: cancelled while running (or during a pause hold) and the
            # marker was consumed by a retry/pause re-entry. This branch used
            # to `return` silently -- no terminal state, no _ops_by_id pop --
            # so the ledger stayed at "retry" forever: the tasks tab showed a
            # task that never ends, has_pending() blocked every auto-submit
            # and cancel_task could not converge the row (the id was in none
            # of _pending/_running/_paused any more). Converge it here, through
            # the same single-fire exit as the _cancelled branch above.
            await self._finalize_cancelled(op)
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
                await self._transition(
                    op, "cancelled", record="cancelled", ledger="cancelled"
                )
                return
            await self._transition(op, "started", ledger="running")
            await self._run_handler(op)
            if op.cancel:
                # Cancelled while running and the handler finished without
                # observing it: either it has no checkpoint in this path, or
                # the cancel landed right before completion. The user's stop
                # request was explicit, so converge the ledger to cancelled
                # instead of reporting "done"/"ok" next to an "interrupted"
                # response (H2: a checkpoint-less kind used to report done).
                await self._transition(
                    op, "cancelled", record="cancelled", ledger="cancelled"
                )
            else:
                await self._transition(op, "done", record="ok", ledger="done")
        except OpCancelError:
            await self._transition(
                op,
                "cancelled",
                target=False,
                record="cancelled",
                ledger="cancelled",
            )
        except OpPausedError:
            # Cooperative pause: hold until resumed (on resume the handler is
            # re-entered from the start, i.e. the task runs again).
            #
            # Register the hold BEFORE the ledger "paused" write. That write is
            # what makes the pause observable to resume_task's caller (the page
            # and tests poll the ledger); if _paused[id] were set only after it,
            # a resume landing inside the window would miss the
            # `task_id in self._paused` branch and fall through to the "running,
            # not yet at a checkpoint" path -- which merely clears op.pause and
            # never requeues, parking the task in "paused" forever. This mirrors
            # the ordering the queued-pause hold already uses in _worker_loop.
            self._paused[op.task_id] = op  # preserves retry count/error; re-entered on resume
            # M2: resume re-enters the handler from its first line, so arm the
            # replay guard here. op.retries must stay untouched: it only counts
            # retries and also gates `op.retries < self._max_retries`, so
            # bumping it here would eat one retry of the budget. Without the
            # flag, handlers deduping on `op.retries > 0` (album replay check,
            # volume segment skip) redo their side effects after pause -> resume.
            op.replayed = True
            keep_index = True
            await self._transition(
                op, "paused", target=False, record="paused", ledger="paused"
            )
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
                op.replayed = True  # M2: re-entry, same replay criterion as pause -> resume
                op.error = str(e)
                backoff = self._backoff_base**op.retries
                logger.warning(
                    f"[op-queue] {op.kind}/{op.task_id} failed ({e}), "
                    f"retry {op.retries}/{self._max_retries} after {backoff}s"
                )
                await self._transition(
                    op,
                    "retry",
                    target=False,
                    ledger="retry",
                    error=str(e),
                    extra={"retries": op.retries, "backoff": backoff},
                )
                # Release resources BEFORE backoff sleep so other workers
                # can acquire them during the cooldown period
                if bulk:
                    self._bulk.release()
                released = True
                # A retrying op is still pending: keep it in the live indexes
                # through the backoff and the requeue wait, or pause_task
                # reports "unknown" for the whole retry lifecycle and a
                # cancel landing inside the sleep is erased by the finally
                # below (the task then runs to completion after the user
                # cancelled it). Same invariants as a freshly submitted op;
                # the worker discards _pending on the next dequeue.
                self._pending.add(op.task_id)
                keep_index = True
                await asyncio.sleep(backoff)
                if high:
                    await self._q_hi.put(op)  # retry keeps its priority (high-priority queue)
                else:
                    await self._q.put(op)
            else:
                logger.error(
                    f"[op-queue] {op.kind}/{op.task_id} failed permanently: {e}"
                )
                await self._transition(
                    op,
                    "failed",
                    target=False,
                    record="failed",
                    ledger="failed",
                    error=str(e),
                    extra={"error": str(e)},
                )
        finally:
            if bulk and not released:
                self._bulk.release()
            self._running.pop(op.task_id, None)
            self._cancelled.discard(op.task_id)
            if not keep_index:
                self._ops_by_id.pop(op.task_id, None)

    async def _finalize_cancelled(self, op: Op) -> None:
        """Converge ``op`` to the cancelled terminal state, exactly once.

        Single-fire guard: cancel_task already wrote the terminal and popped
        the index for a task it caught while queued / pause-held (deep-queue
        write-through). Writing it again here would double the SSE event and
        the recent record. Only finalize when the cancel landed after this op
        left _ops_by_id's control (i.e. between dequeue and here): membership
        in _ops_by_id means no terminal state has been written for it yet.
        """
        if op.task_id not in self._ops_by_id:
            return
        self._ops_by_id.pop(op.task_id, None)
        await self._transition(
            op, "cancelled", record="cancelled", ledger="cancelled"
        )

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
