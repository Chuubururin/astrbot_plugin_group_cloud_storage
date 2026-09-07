"""OpQueue -- unified rate-limited operation queue.

All external operations initiated from the page (scan/rename/upload/delete/
move/sync) are queued and executed through the RateLimiter port (interval +
backoff) to withstand QQ rate control; SSE subscribers receive progress in
real time.

Responsibility split (this file only composes mixins and handles submit/status):
- op.py        Op model, queue exceptions, bulk/priority kind sets
- execution.py worker pool, rate-limit acquisition, retry/backoff
- control.py   pause/resume/cancel/interrupt and task ledger integration
- events.py    SSE event publishing and subscription

Conventions:
- The execution function is injected by the caller (run_handler); OpQueue only
  schedules and rate-limits (single responsibility)
- Retry: retriable exceptions (OneBotErrorKind.TIMEOUT/RATE_LIMITED/REMOTE_ERROR)
  back off exponentially
- Cancel: once op.cancel is set the op is skipped; the handler owns writing
  the terminal state
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Awaitable, Callable

from ports.limiter import NullLimiter, RateLimiter

from .control import TaskControlMixin
from .events import SseEventsMixin
from .execution import ExecutionMixin
from .op import DEFAULT_HIGH_PRIORITY, Op


class OpQueue(TaskControlMixin, ExecutionMixin, SseEventsMixin):
    def __init__(
        self,
        run_handler: Callable[[Op], Awaitable[None]],
        interval: float = 0.5,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        limiter: RateLimiter | None = None,
        high_priority: set[str] | None = None,
        slots: int = 4,
        ledger=None,  # task ledger hooks (on_state/on_op, see TaskControlService)
    ):
        self._run_handler = run_handler
        self._high_priority = (
            high_priority if high_priority is not None else DEFAULT_HIGH_PRIORITY
        )
        # Rate limiting is injected via the RateLimiter port (bootstrap wires a
        # KeyedLimiter keyed by account); a no-op implementation is used when
        # nothing is injected (tests/minimal deployments). The interval parameter
        # is kept for backward compatibility with existing callers; pacing is
        # fully determined by the injected RateLimiter.
        self._limiter = limiter if limiter is not None else NullLimiter()
        # Bulk operations: non-interactive bulk kinds (volume conversion/video
        # processing/fetch/netdisk export) are concurrency-capped, not
        # rate-limited; QQ calls are rate-limited per account by the adapter
        # (cross-account concurrency)
        self._bulk = asyncio.Semaphore(2)
        self._slots = max(2, int(slots))
        self._q_hi: asyncio.Queue = asyncio.Queue()
        self._q: asyncio.Queue = asyncio.Queue()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._listeners: set["asyncio.Queue[dict]"] = set()
        self._pending: set[str] = set()  # submitted, not yet executing (queued or waiting)
        self._cancelled: set[str] = set()  # cancelled tasks (checked on dequeue)
        # Task ID -> op index, avoids scanning all running tasks on cancel; the
        # index covers the full lifecycle: queued, rate-limit wait, running,
        # and pause hold.
        self._ops_by_id: dict[str, Op] = {}
        self._paused: dict[str, Op | None] = {}  # pause holds (None placeholder = still in queue)
        self._workers: list[asyncio.Task] = []
        self._shutting_down = False
        self._running: dict[str, Op] = {}
        self._recent: deque[dict] = deque(maxlen=20)
        self._lock = asyncio.Lock()
        self._ledger = ledger  # task ledger (on_state/on_op); None = no ledger writes

    async def submit(
        self, kind: str, target: str = "", payload: dict | None = None, account=None
    ) -> str:
        await (
            self.start()
        )  # idempotent: workers are resident, so every submit is consumed
        op = Op(
            task_id=uuid.uuid4().hex[:12],
            kind=kind,
            target=target,
            payload=payload or {},
            account=account,
        )
        self._pending.add(op.task_id)
        self._ops_by_id[op.task_id] = op
        # Write the pending ledger state BEFORE enqueueing: ledger writes go
        # through to_thread and yield to the event loop, so if the write ran
        # after the enqueue, a very fast task could finish before the pending
        # state is committed, and the stale pending write would land last and
        # overwrite the terminal state.
        await self._ledger_state(op, "pending")
        if kind in self._high_priority:
            await self._q_hi.put(op)
        else:
            await self._q.put(op)
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

    def has_pending(self, kind: str, payload_key: str, payload_value) -> bool:
        """True if a queued/running op of ``kind`` already carries
        payload[payload_key] == payload_value (dedup for auto-submits)."""
        for op in self._ops_by_id.values():
            if op.kind == kind and op.payload.get(payload_key) == payload_value:
                return True
        return False

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
