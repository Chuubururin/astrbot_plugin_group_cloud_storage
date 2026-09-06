"""SubmitMixin — outbound transfer submission methods."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

class SubmitMixin:
    """Queue async submission for bridge tasks ."""

    async def submit_out(
        self,
        group_id: str,
        resource_id: int,
        *,
        dst_dir: str | None = None,
        force: bool = False,
    ) -> str:
        """Submit bridge_out task (group file -> OpenList)."""
        tid = await self._queue.submit(
            "bridge_out",
            target=group_id,
            payload={
                "resource_id": resource_id,
                "dst_dir": dst_dir,
                "force": force,
            },
        )
        self._ensure_poll_task()
        return tid

    async def submit_in(self, path: str, *, group_id: str) -> str:
        """Submit bridge_in task (OpenList -> group file)."""
        tid = await self._queue.submit(
            "bridge_in",
            target=group_id,
            payload={"path": path},
        )
        self._ensure_poll_task()
        return tid
