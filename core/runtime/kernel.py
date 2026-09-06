"""Runtime kernel — central state holder.

Holds: Services, task registry, event bus, current state and generation.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commands.handlers import Services


class RuntimeKernel:
    """Central runtime state holder."""

    def __init__(self, services: "Services"):
        self.services = services
        self._tasks: set[asyncio.Task] = set()
        self._generation: int = 0
        self._inited: bool = False

    @property
    def generation(self) -> int:
        return self._generation

    def track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
