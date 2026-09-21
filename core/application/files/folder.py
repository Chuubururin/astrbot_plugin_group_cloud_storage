"""Folder create/list methods."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.log import logger

if TYPE_CHECKING:
    from .service import FileOpsService


async def folder_exists(
    api, group_id: str, name: str, parent_id: str = "/"
) -> bool:
    """Whether the group already has a file folder called *name*.

    Consulted on replays only (a queue retry or a pause -> resume re-runs the
    handler from its first line). ``create_group_file_folder`` is not
    idempotent: replaying it creates a *second* folder with the same name, and
    QQ shows both. Mirrors ingest/video.py's ``remote_has_file`` convention --
    the happy path never pays for the extra listing.

    Returns False when the listing itself fails (no probe channel, e.g. stub
    adapters): the caller then creates, i.e. the pre-guard behaviour, so a
    probe outage degrades to a duplicate rather than a missing folder.
    """
    wanted = (name or "").strip()
    if not wanted:
        return False
    try:
        if parent_id and parent_id != "/":
            lst = await api.list_group_folder(group_id, parent_id)
        else:
            lst = await api.list_group_root(group_id)
    except Exception:
        return False
    for fd in getattr(lst, "folders", None) or []:
        if str(getattr(fd, "name", "") or "").strip() == wanted:
            return True
    return False


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
        name = op.payload["name"]
        parent_id = op.payload.get("parent_id", "/")
        # Replay guard: a retry / pause -> resume re-enters this handler
        # from its first line, and create_group_file_folder is not
        # idempotent -- without this the group gets two folders of the same
        # name. Only consulted on a replay, so the happy path keeps its
        # single create round trip.
        if bool(getattr(op, "replayed", False)) and await folder_exists(
            self.api, op.target, name, parent_id
        ):
            logger.info(
                f"[file-ops] folder replay: {name} already in "
                f"{op.target}, skipping re-create"
            )
        else:
            await self.api.create_group_file_folder(op.target, name, parent_id)
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
