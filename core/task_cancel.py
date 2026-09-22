"""Bounded cancellation of a set of asyncio tasks (shared teardown primitive).

Teardown must have a *real* upper bound. Three shapes look like bounds but are
not:

* ``await asyncio.sleep(timeout)`` as a grace period.  It never early-exits, so
  every teardown paid the full grace even when the tasks were already dead
  microseconds later (measured on ``OpQueue.shutdown``: exactly 1.0 s per
  shutdown, which turned a 77 s suite into 326 s and broke timing assertions in
  tests/contract).
* ``await asyncio.wait_for(...)``.  On CPython <= 3.11 the timeout path is
  ``_cancel_and_wait`` -> ``task.cancel()`` followed by ``await waiter``, i.e.
  it waits for the task to *finish* after cancelling it.  A task that absorbs
  the cancellation never finishes, so ``wait_for`` is not a bound at all.
  (3.12+ reimplemented ``wait_for`` on top of ``timeouts.timeout`` and lost this
  hole, which is why the hang reproduced in CI on 3.10 but not locally on 3.13.)
* ``await asyncio.gather(*tasks, return_exceptions=True)`` with no timeout.  It
  simply never returns while one task is still running.

``asyncio.wait`` returns as soon as the tasks are done *and* honours the
deadline, so **deadline + re-cancel** is a real bound: a task that absorbs the
first ``CancelledError`` gets another one at every slice until the deadline, and
is then abandoned with a warning instead of hanging the caller.

Callers: ``OpQueue.shutdown()`` (worker pool) and ``RuntimeKernel.cancel_all()``
(plugin teardown).  The algorithm lives in exactly one place on purpose -- it
was duplicated once and the two copies drifted (only the queue side had the
re-cancel round).
"""

from __future__ import annotations

import asyncio

from core.log import logger

CANCEL_SLICE = 0.05
"""重发取消的切片上限（秒）。

每次等待都不得超过它，否则一个"扛住第一次取消"的任务会独占整个宽限期，
使重发取消那一轮因 ``remaining <= 0`` 而一次都不执行（实测过）。
0.05 的取舍：快路径（任务都停泊在可取消点）下 ``asyncio.wait`` 会在任务一
结束就返回，切片刻不产生额外等待；慢路径下每秒可重发约 20 次。
"""


async def cancel_tasks(
    tasks: list[asyncio.Task], *, timeout: float = 1.0, label: str = "task"
) -> list[asyncio.Task]:
    """Cancel ``tasks``, wait for them, and give up at ``timeout``.

    Returns the tasks still alive at the deadline (abandoned).  The caller is
    never blocked past ``timeout``; abandoned tasks are logged so a leak is
    visible instead of silent.
    """
    if not tasks:
        return []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    # Round 1: a task parked at a cancellable point dies here.  A task that is
    # mid-handler only *absorbs* the flag (it is consumed the next time the
    # handler suspends), so survivors need another round.
    for t in tasks:
        t.cancel()
    await asyncio.wait(tasks, timeout=min(timeout, CANCEL_SLICE))

    # Round 2..n: re-cancel whatever is still alive until the deadline.
    for t in tasks:
        while not t.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    f"[{label}] {t.get_name()} survived cancellation; "
                    f"abandoning it after {timeout}s"
                )
                break
            t.cancel()
            await asyncio.wait({t}, timeout=min(remaining, CANCEL_SLICE))
    return [t for t in tasks if not t.done()]
