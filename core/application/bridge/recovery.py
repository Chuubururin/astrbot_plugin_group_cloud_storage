"""RecoveryMixin — retry, cancel, and recovery methods."""
from __future__ import annotations

from adapters.external.base import ExternalApiError, normalize_task_state
from core.domain.enums import BridgeTaskState
from core.log import logger


class RecoveryMixin:
    """Startup recovery and task control."""

    async def recover(self) -> None:
        """Startup recovery scan.

        One-time undone/done reconciliation for pending/running entries.
        """
        rows = await self._store.list_archive_map(
            states=(
                BridgeTaskState.PENDING.value,
                BridgeTaskState.RUNNING.value,
                BridgeTaskState.UNKNOWN.value,
            ),
            direction="out",
        )

        if rows:
            logger.info(
                f"[bridge] recovery: {len(rows)} pending/running/unknown entries"
            )
        try:
            undone = (
                {t.id: t for t in await self._client.tasks_undone()} if rows else {}
            )
            done = {t.id: t for t in await self._client.tasks_done()} if rows else {}

            for row in rows:
                task = undone.get(row["task_id"]) or done.get(row["task_id"])
                if task is None:
                    # Task not in either list; try stat probe
                    stat = await self._client.stat(row["remote_path"])
                    state = (
                        BridgeTaskState.DONE.value
                        if stat
                        else BridgeTaskState.FAILED.value
                    )
                else:
                    state = normalize_task_state(task.state)

                await self._store.update_archive_state(row, state)
                if state == BridgeTaskState.DONE.value:
                    # Rename the UUID filename to the intended name
                    await self._maybe_rename_to_intended(row)
                    await self._notify_group(row, state)
                elif state == BridgeTaskState.FAILED.value:
                    await self._notify_group(row, state)
        except ExternalApiError as e:
            logger.warning(f"[bridge] recovery failed: {e.message}")

        # Zombie pending_in convergence :
        # fetch ops live only in the in-memory OpQueue, so after a restart
        # their completion events are gone -- pending in-rows can never
        # advance. Converge them to failed so the ledger stays queryable.
        try:
            in_rows = await self._store.list_archive_map(
                states=(
                    BridgeTaskState.PENDING.value,
                    BridgeTaskState.RUNNING.value,
                    BridgeTaskState.UNKNOWN.value,
                ),
                direction="in",
            )
            for row in in_rows:
                await self._store.update_archive_state(
                    row, BridgeTaskState.FAILED.value
                )
            if in_rows:
                logger.warning(
                    f"[bridge] recovery: converged {len(in_rows)} orphaned "
                    "pending_in rows to failed (fetch ops do not survive restart)"
                )
        except ExternalApiError as e:
            logger.warning(f"[bridge] pending_in convergence failed: {e.message}")

        # If interval > 0 and tasks remain, start polling
        if self._interval > 0:
            remaining = await self._store.list_archive_map(
                states=(BridgeTaskState.PENDING.value, BridgeTaskState.RUNNING.value),
                direction="out",
            )
            if remaining:
                self._ensure_poll_task()

    async def cancel(self, task_id: str) -> bool:
        """Cancel an OpenList task."""
        try:
            return await self._client.task_cancel(task_id)
        except ExternalApiError as e:
            logger.warning(f"[bridge] cancel failed: {e.message}")
            return False

    async def retry(self, task_id: str) -> bool:
        """Retry a failed OpenList task."""
        try:
            return await self._client.task_retry(task_id)
        except ExternalApiError as e:
            logger.warning(f"[bridge] retry failed: {e.message}")
            return False
