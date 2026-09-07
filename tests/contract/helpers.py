"""Shared helpers for contract tests."""

from __future__ import annotations

import asyncio
import time


async def drain_op(queue, task_id, timeout: float = 10.0):
    """Wait for an operation to appear in the queue's recent results."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await queue.status()
        recent = [row for row in status["recent"] if row["task_id"] == task_id]
        if recent:
            return recent[0]
        await asyncio.sleep(0.05)
    raise TimeoutError("op not finished")
