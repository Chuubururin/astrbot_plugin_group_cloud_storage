"""Task control (pause/resume/interrupt/undo) and task ledger integration --
OpQueue responsibility slice.

State attributes (_pending/_cancelled/_paused/_running/_ops_by_id/_ledger)
are created in OpQueue.__init__.
"""

from __future__ import annotations

import asyncio
import time

from core.log import logger

from .op import Op, OpCancelError, OpPausedError


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks: log exceptions instead of
    silently discarding them."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(f"[queue] fire-and-forget task error: {exc}")


class TaskControlMixin:
    # ---------- Task control (pause/resume/interrupt/undo) ----------

    def pause_task(self, task_id: str) -> str:
        """Pause a task (cooperative). Synchronous method -- pure in-memory
        state change; call directly, do not await.

        Returns:
            "queued": held while queued
            "running": running, takes effect at the next checkpoint
            "paused": already paused
            "unknown": task not found
        """
        if task_id in self._paused:
            return "paused"
        if task_id in self._pending:
            self._paused[task_id] = None  # queued: placeholder, held when the worker dequeues
            # Ledger must reflect the pause immediately: the worker only
            # rewrites the ledger when it dequeues the op, which may be much
            # later (deep queue). Without this write the tasks tab keeps
            # showing the pre-click state ("pending") after the user paused
            # a queued task.
            op = self._ops_by_id.get(task_id)
            if op is not None:
                self._ledger_fire(op, "paused")
            self._push({"type": "paused", "task_id": task_id, "ts": time.time()})
            return "queued"
        op = self._running.get(task_id)
        if op is not None:
            op.pause = True  # running: takes effect at the next handler checkpoint (pause_check)
            return "running"
        return "unknown"

    def resume_task(self, task_id: str) -> str:
        """Resume a task. Synchronous method -- pure in-memory state change;
        call directly, do not await.

        Returns:
            "resumed": resumed
            "unknown": task not found or not paused
        """
        if task_id in self._paused:
            entry = self._paused.pop(task_id)
            if entry is not None:  # a real op held after dequeue: re-enqueue by priority
                entry.pause = False
                # Re-register as pending before the requeue: the worker only
                # discards it on the next dequeue. Without this the op waits in
                # the async queue outside every live index, so pause_task
                # reports "unknown" for the whole requeue window and cancel_task
                # skips its terminal write-through (the row stays "pending"
                # until the worker finally dequeues). Same invariant as submit()
                # and the retry requeue in execution._execute.
                self._pending.add(entry.task_id)
                if entry.kind in self._high_priority:
                    self._q_hi.put_nowait(entry)
                else:
                    self._q.put_nowait(entry)
                self._ledger_fire(entry, "pending")
            else:
                # Queued placeholder: pause already rewrote the ledger to
                # "paused" (deep-queue reasoning in pause_task); resume must
                # write through symmetrically or the row stays "paused" until
                # the worker dequeues — hours behind a scan wave (live
                # 2026-09-12: resume→interrupt left the tasks tab "paused").
                # The _pending guard skips the fire if the worker is dequeuing
                # right now: that path writes running/done itself, and a late
                # "pending" would only be transient (superseded on terminal).
                op = self._ops_by_id.get(task_id)
                if op is not None and task_id in self._pending:
                    self._ledger_fire(op, "pending")
        else:
            # Running task paused but not yet at a checkpoint: pause_task only
            # set op.pause (no _paused entry), so clear the flag here or this
            # resume click is lost and the task parks until the user clicks
            # resume a second time (live: pause→resume on a running task
            # reported "unknown" and the task stayed halted).
            op = self._running.get(task_id)
            if op is None or not op.pause:
                return "unknown"
            op.pause = False
        self._push({"type": "resumed", "task_id": task_id, "ts": time.time()})
        return "resumed"

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task: covers queued / rate-limit wait / pause hold /
        running (running cancellation is cooperative via the handler).

        Synchronous method -- pure in-memory state change; call directly, do
        not await.
        """
        op = self._ops_by_id.get(task_id)
        hit = op is not None
        if task_id in self._paused:
            self._pending.discard(task_id)
            held = self._paused.pop(task_id, None)
            hit = True
            if held is not None:  # a real held op: set terminal state directly (not in any queue)
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
                self._ops_by_id.pop(task_id, None)
            elif op is not None:
                # Queued placeholder: the op object is still inside the async
                # queue and may not be dequeued for a long time (deep queue),
                # so finalize the ledger now. Its later dequeue lands in the
                # _cancelled branch of _execute, which discards and re-writes
                # the same terminal state (idempotent).
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
                self._ledger_fire(op, "cancelled")
                self._ops_by_id.pop(task_id, None)
        # Only track cancels for ops that are still live in the queue (queued /
        # retry wait -> _pending, or running -> _running): those are observed on
        # dequeue and discarded in _execute's finally. Unknown/terminal ids and
        # pause-held ops (no longer in any queue) would never be discarded,
        # growing _cancelled without bound.
        if op is not None and (
            task_id in self._pending or task_id in self._running
        ):
            self._cancelled.add(task_id)
        if op is not None:
            op.cancel = True
            if task_id in self._pending:
                # Queued (not pause-held): the worker may not dequeue for a
                # long time (deep queue), so write the terminal state now —
                # the later dequeue lands in the _execute cancelled branch,
                # which discards and re-writes the same state (idempotent,
                # same reasoning as the pause-held branch above).
                self._pending.discard(task_id)
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
                self._ledger_fire(op, "cancelled")
                self._ops_by_id.pop(task_id, None)
        return hit

    def interrupt_task(self, task_id: str) -> bool:
        """Interrupt a task: an alias for cancel (removed if queued;
        cooperative cancel if running; terminal state if held paused).

        Synchronous method -- no await points, same semantics as cancel_task;
        the name is kept for compatibility."""
        return self.cancel_task(task_id)

    async def pause_check(self, op: Op) -> None:
        """Cooperative checkpoint: handlers call it between OneBot calls;
        cancelled -> OpCancelError; paused -> OpPausedError."""
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
        """Operation log entry: before/after snapshots of reversible
        operations, used for undo compensation.
        """
        if self._ledger is not None:
            await self._ledger.on_op(task_id, action, before, after)

    # ---------- Ledger integration ----------

    async def checkpoint_payload(self, op: Op) -> None:
        """Persist a handler's mid-run ``op.payload`` mutations.

        The ledger is otherwise written only at state transitions
        (running/paused/retry/failed/done in execution.py), so the payload
        snapshot taken by the *running* write is what a restart sees. Any key
        a handler adds while running is therefore lost on a crash -- a pause
        survives (the paused write captures it), a kill/restart does not.

        Two live defects came from that gap: essence_save's ``sent_parts``
        ledger was discarded, so a crash mid-document re-sent every part and
        orphaned the earlier messages (their ids lived only in memory, so the
        plugin could no longer delete them); and upload's staged
        ``parent_resource_id`` was discarded, so a crash between creating the
        volume parent row and finishing the upload made the retry create a
        second parent and orphan the first.

        Handlers call this immediately after a non-idempotent side effect
        whose "already done" marker they keep in the payload. Reuses the
        ``running`` state so no new state (or schema) is introduced; the
        write is an upsert, so calling it repeatedly is safe.
        """
        await self._ledger_state(op, "running")

    async def _ledger_state(
        self, op: Op, state: str, error: str | None = None
    ) -> None:
        if self._ledger is None:
            return
        await self._ledger.on_state(
            op.task_id, op.kind, op.target, op.payload, state, error
        )

    def _ledger_fire(self, op: Op, state: str, error: str | None = None) -> None:
        """Record ledger state asynchronously from a synchronous context
        (cancel/pause paths, called inside the event loop).
        """
        if self._ledger is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        # BUG-9 fix: track fire-and-forget task to log exceptions instead of
        # silently swallowing them ("Task exception was never retrieved").
        task = asyncio.create_task(
            self._ledger.on_state(
                op.task_id, op.kind, op.target, op.payload, state, error
            )
        )
        task.add_done_callback(_log_task_exception)


__all__ = ["TaskControlMixin"]
