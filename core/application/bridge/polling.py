"""PollingMixin — task polling and status checking methods."""
from __future__ import annotations

import asyncio

from adapters.external.base import ExternalApiError, normalize_task_state
from core.domain.enums import BridgeTaskState
from core.log import logger
from core.application.bridge import _now, _split_ext, _short_suffix


class PollingMixin:
    """Background polling and task-driven status checks."""

    async def handle_bridge_out(self, op) -> None:
        """Handle bridge_out operation (seven steps)."""
        gid = op.target
        rid = op.payload["resource_id"]

        # Step 1: dlserver guard 
        if not (self._dlserver.enabled and self._dlserver.http_port > 0):
            return self._fail(op, "download_server_disabled")

        # Step 2: Size window
        res = await self._store.get_resource_detail(gid, rid)
        if res is None:
            return self._fail(op, "not_found")
        size = int(res.get("size") or 0)
        if not self._size_ok(size):
            return self._fail(op, "size_filtered")

        # Step 3: Idempotency + remote probe 
        row = await self._store.get_archive_map(gid, rid, direction="out")
        if row and not op.payload.get("force"):
            # Check if row state is already done (idempotency)
            if row.get("state") == BridgeTaskState.DONE.value:
                # Verify file still exists at remote path
                stat = await self._client.stat(row["remote_path"])
                if stat:
                    return self._done(op, skipped=True, remote_path=row["remote_path"])
                # File was deleted remotely, clear and re-submit
                await self._store.clear_archive_map(gid, rid, "out")
            else:
                # Task is pending/running, don't re-submit
                return self._done(
                    op,
                    skipped=True,
                    remote_path=row["remote_path"],
                    detail=f"task in progress (state={row.get('state')})",
                )

        # Step 4: Target path (literal replace, no str.format)
        dst_dir = op.payload.get("dst_dir") or self._dst_dir
        remote_dir, remote_path = self._render_dst(dst_dir, gid, res["name"])

        # Conflict resolution: stat(remote_path) hit + no same-path diff-resource
        existing = await self._client.stat(remote_path)
        if existing:
            short_id = _short_suffix()
            name_part = res["name"]
            base, ext = _split_ext(name_part)
            remote_path = f"{remote_dir}/{base}_{short_id}{ext}"

        # Step 5: Idempotent mkdir 
        await self._client.mkdir(remote_dir)

        # Step 6: File source direct link 
        url = self._dlserver.download_url(gid, rid)

        # Step 7: Control plane submit + ledger
        try:
            tasks = await self._client.submit_offline_download([url], remote_dir)
        except ExternalApiError as e:
            return self._fail(op, f"submit_failed: {e.message}")

        task_id = tasks[0].id if tasks else ""
        await self._store.upsert_archive_map(
            {
                "resource_id": rid,
                "group_id": gid,
                "task_id": task_id,
                "remote_path": remote_path,
                "direction": "out",
                "state": BridgeTaskState.PENDING.value,
                "updated_at": _now(),
            }
        )
        self._publish(op, state="pending", percent=0.0)
        logger.info(f"[bridge] bridge_out submitted: {gid}/{rid} -> {remote_path}")

    # -- Background polling  --

    def _ensure_ledger_task(self) -> None:
        """Lazy start the ledger consumer .

        Runs only while tracked bridge_in fetch tasks exist; exits by itself
        when the tracked set drains and no pending in-rows remain.
        """
        if self._ledger_task is not None and not self._ledger_task.done():
            return
        self._ledger_task = asyncio.create_task(
            self._queue_ledger_task(), name="bridge-ledger"
        )
        logger.info("[bridge] ledger task started")

    async def read_repair_row(self, row: dict) -> None:
        """Read repair for one ledger row: reconcile against OpenList.

        Mirrors the poll-loop decision tree (undone list -> done list ->
        stat probe); state changes are persisted and renamed on done. Best
        effort: ExternalApiError leaves the row untouched.
        """
        task_id = row.get("task_id", "")
        try:
            undone = {t.id: t for t in await self._client.tasks_undone()}
            done = {t.id: t for t in await self._client.tasks_done()}
            task = undone.get(task_id) or done.get(task_id)
            if task is None:
                stat = await self._client.stat(row.get("remote_path", ""))
                if not stat:
                    return  # still in flight (or retry window) — keep pending
                state = BridgeTaskState.DONE.value
            else:
                state = normalize_task_state(task.state)
            if state == row.get("state"):
                return
            if state == BridgeTaskState.DONE.value:
                await self._maybe_rename_to_intended(row)
            await self._store.update_archive_state(row, state)
            self._publish(row, state, percent=100.0 if task is None else task.progress)
        except ExternalApiError as e:
            logger.warning(f"[bridge] read repair failed ({task_id}): {e.message}")

    async def read_repair_pending(self) -> None:
        """Read repair for all actionable out rows (manual mode aggregate)."""
        rows = await self._store.list_archive_map(
            states=(
                BridgeTaskState.PENDING.value,
                BridgeTaskState.RUNNING.value,
                BridgeTaskState.UNKNOWN.value,
            ),
            direction="out",
        )
        for row in rows:
            await self.read_repair_row(row)

    def _ensure_poll_task(self) -> None:
        """Lazy create poll task; interval=0 means manual mode."""
        if self._interval <= 0:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(self._poll_loop(), name="bridge-poll")
        logger.info("[bridge] poll task started")

    async def stop_polling(self) -> None:
        """Stop poll and ledger tasks ."""
        self._stopping = True
        for attr in ("_poll_task", "_ledger_task"):
            task = getattr(self, attr)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, attr, None)

    async def _poll_loop(self) -> None:
        """Poll loop: check undone/done lists, update ledger."""
        backoff: dict[str, int] = {}

        while not self._stopping:
            rows = await self._store.list_archive_map(
                states=(
                    BridgeTaskState.PENDING.value,
                    BridgeTaskState.RUNNING.value,
                    BridgeTaskState.UNKNOWN.value,
                ),
                direction="out",
            )
            in_pending = await self._in_pending()

            if not rows and not in_pending:
                # No unfinished tasks -> auto stop 
                self._poll_task = None
                logger.info("[bridge] poll task stopped: no pending tasks")
                return

            await asyncio.sleep(self._interval)

            try:
                undone = {t.id: t for t in await self._client.tasks_undone()}
                done = {t.id: t for t in await self._client.tasks_done()}

                for row in rows:
                    task = undone.get(row["task_id"]) or done.get(row["task_id"])
                    if task is None:
                        # Double list missing -> backoff probe 
                        n = backoff.get(row["task_id"], 0)
                        backoff[row["task_id"]] = n + 1
                        # Poll ticks are ~10s apart; probe stat() only every
                        # 3rd miss so the remote check runs at ~30s intervals
                        # (10s -> 30s -> capped), not on every tick.
                        if n % 3:
                            continue
                        stat = await self._client.stat(row["remote_path"])
                        if not stat:
                            continue
                        state = "done"  # Remote exists but task gone -> done
                    else:
                        state = normalize_task_state(task.state)

                    # Rename the UUID filename to the intended name on completion
                    if state == BridgeTaskState.DONE.value:
                        await self._maybe_rename_to_intended(row)

                    await self._store.update_archive_state(row, state)
                    self._publish(
                        row,
                        state,
                        percent=task.progress if task else 100.0,
                    )
                    if state in (
                        BridgeTaskState.DONE.value,
                        BridgeTaskState.FAILED.value,
                    ):
                        await self._notify_group(row, state)

            except ExternalApiError as e:
                logger.warning(f"[bridge] poll error: {e.message}")

    async def _queue_ledger_task(self) -> None:
        """Consume queue events for bridge_in ledger linkage .

        Terminal events only: done -> done; failed/cancelled -> failed (the
        queue's done event carries no error field; failures arrive as their
        own terminal events). Exits when tracked ids and pending in-rows drain.
        """
        try:
            async for ev in self._queue.subscribe():
                tid = ev.get("task_id", "")
                if tid not in self._in_task_ids:
                    continue
                ev_type = ev.get("type", "")
                if ev_type == "done":
                    state = BridgeTaskState.DONE.value
                elif ev_type in ("failed", "cancelled"):
                    state = BridgeTaskState.FAILED.value
                else:
                    continue
                await self._store.update_archive_state_by_task(tid, state)
                self._in_task_ids.discard(tid)
                if not self._in_task_ids and not await self._in_pending():
                    return
        finally:
            self._ledger_task = None

    async def _in_pending(self) -> bool:
        """Check if there are pending bridge_in tasks."""
        rows = await self._store.list_archive_map(
            states=(BridgeTaskState.PENDING.value, BridgeTaskState.RUNNING.value),
            direction="in",
        )
        return len(rows) > 0

    async def status(self, task_id: str | None = None) -> dict:
        """Query task status (single task by id, or aggregate counters).

        Read repair: with auto-polling disabled (interval=0) nothing else
        converges pending out-rows, so a status read reconciles against
        OpenList first (single row or the pending set). Polling-enabled
        deployments skip this: the poll loop is the converger.
        """
        if task_id:
            row = await self._store.get_archive_map_by_task(task_id)
            if row is None:
                return {"task_id": task_id, "state": BridgeTaskState.UNKNOWN.value}
            if self._interval <= 0 and row.get("state") in (
                BridgeTaskState.PENDING.value,
                BridgeTaskState.RUNNING.value,
                BridgeTaskState.UNKNOWN.value,
            ):
                await self.read_repair_row(row)
                row = (
                    await self._store.get_archive_map_by_task(task_id)
                    or row
                )
            return {
                "task_id": task_id,
                "state": row.get("state", BridgeTaskState.UNKNOWN.value),
                "direction": row.get("direction", ""),
                "group_id": row.get("group_id", ""),
                "remote_path": row.get("remote_path", ""),
                "updated_at": row.get("updated_at", ""),
            }

        if self._interval <= 0:
            await self.read_repair_pending()

        rows_out = await self._store.list_archive_map(
            states=(
                BridgeTaskState.PENDING.value,
                BridgeTaskState.RUNNING.value,
                BridgeTaskState.DONE.value,
                BridgeTaskState.FAILED.value,
            ),
            direction="out",
        )
        rows_in = await self._store.list_archive_map(
            states=(
                BridgeTaskState.PENDING.value,
                BridgeTaskState.RUNNING.value,
                BridgeTaskState.DONE.value,
                BridgeTaskState.FAILED.value,
            ),
            direction="in",
        )
        return {
            "enabled": True,
            "capability": self._client.capability,
            "dlserver_ready": self._dlserver.enabled and self._dlserver.http_port > 0,
            "tasks_out": len(rows_out),
            "tasks_in": len(rows_in),
            "pending_out": sum(
                1
                for r in rows_out
                if BridgeTaskState.is_actionable(BridgeTaskState(r["state"]))
            ),
            "pending_in": sum(
                1
                for r in rows_in
                if BridgeTaskState.is_actionable(BridgeTaskState(r["state"]))
            ),
        }
