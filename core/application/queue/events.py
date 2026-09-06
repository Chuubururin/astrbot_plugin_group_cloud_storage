"""SSE event stream -- subscription/publishing slice of OpQueue.

OpQueue composes this module as a mixin; state attributes (_listeners) are
created in OpQueue.__init__. Event shape: {type, task_id, kind, target?, ts,
...}, constructed explicitly by each responsibility slice.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator


class SseEventsMixin:
    """Event publishing and subscription (publish/subscribe for handlers and
    the Web API).
    """

    def publish(self, ev: dict) -> None:
        """Public publish: lets op handlers emit intra-task progress
        (e.g. scan i/N).
        """
        self._push({**ev, "ts": time.time()})

    def _push(self, ev: dict) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[dict]:
        """Subscribe to the event stream (the consumer must close it on exit)."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._listeners.add(q)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            self._listeners.discard(q)


def op_event(type_: str, op, **extra) -> dict:
    """Uniform event with op context (task_id/kind/target + timestamp)."""
    return {
        "type": type_,
        "task_id": op.task_id,
        "kind": op.kind,
        "target": op.target,
        **extra,
        "ts": time.time(),
    }


__all__ = ["SseEventsMixin", "op_event"]
