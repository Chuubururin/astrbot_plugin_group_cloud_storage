"""Folder create/list methods."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.log import logger

if TYPE_CHECKING:
    from .service import FileOpsService


class FolderMixin:
    if TYPE_CHECKING:
        _service: FileOpsService

    async def submit_create_folder(self, group_id: str, name: str, parent_id: str = "/") -> str:
        """Create a group file folder (delegates to create_group_file_folder)."""
        if not (0 < len(name) <= 60):
            raise ValueError("folder name length 1..60")
        return await self.queue.submit(
            "create_folder",
            target=group_id,
            payload={"name": name, "parent_id": parent_id or "/"},
        )

    async def _do_create_folder(self, op) -> None:
        await self.api.create_group_file_folder(
            op.target, op.payload["name"], op.payload.get("parent_id", "/")
        )
        # Refresh folder entities (immediately visible in listings/dropdowns)
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if not result.ok:
            # BUG-9: a concurrent sync on the same group (another folder
            # creation, an upload, a manual scan) makes run_full_sync reject
            # without scheduling anything, so the new folder row would be
            # missing until the next periodic sync. Retry after a short
            # backoff (Celery-style: transient contention -> retry with
            # backoff; full sync is idempotent so a re-run is safe), and
            # only log when both attempts hit contention.
            await asyncio.sleep(3.0)
            result = await self.sync.run_full_sync(op.target, lock)
        if not result.ok:
            logger.warning(f"[file-ops] post-folder sync failed: {result.error}")
        logger.info(f"[file-ops] folder created: {op.payload['name']} in {op.target}")
