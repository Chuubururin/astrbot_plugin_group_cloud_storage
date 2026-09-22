"""Runtime kernel — central state holder.

Holds: Services, task registry, event bus, current state and generation.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.task_cancel import cancel_tasks

if TYPE_CHECKING:
    from commands.handlers import Services


class RuntimeKernel:
    """Central runtime state holder."""

    def __init__(self, services: "Services") -> None:
        self.services = services
        self._tasks: set[asyncio.Task] = set()
        self._generation: int = 0

    @property
    def generation(self) -> int:
        return self._generation

    def track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_all(self, timeout: float = 1.0) -> None:
        """Cancel every tracked task and wait for them -- bounded by ``timeout``.

        ``asyncio.gather(*self._tasks, return_exceptions=True)`` (the previous
        implementation) has no deadline: a task that absorbs the first
        ``CancelledError`` never finishes, so gather never returns and
        ``LifecycleManager.terminate()`` -- the plugin disable / reload path --
        hangs forever.  Same defect class as the queue's S-1, so it uses the
        same primitive (``core.task_cancel.cancel_tasks``).

        The registry is cleared *before* the wait: a task spawned during
        teardown then stays tracked instead of being wiped unseen, which is what
        the old ``clear()``-after-gather ordering did.
        """
        tasks = list(self._tasks)
        self._tasks.clear()
        if not tasks:
            return
        await cancel_tasks(tasks, timeout=timeout, label="kernel")
