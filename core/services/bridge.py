"""BridgeService -- OpenList bridge orchestration (~300 lines).

Implements:
- REQ-01: Zero disk IO on forward path (only generate/submit links)
- REQ-07: Idempotency (stat probe, 405 tolerance, conflict resolution)
- REQ-08: Async queue (user-triggered via OpQueue, polling is background task)
- REQ-10: Task-driven polling (only runs when tasks exist)
- REQ-16: dlserver guard (enabled + port check)
- REQ-17: Ledger decoupled from resources.status
- REQ-18: Auto behavior grading (default manual, B-class necessary auto)

Dependencies:
- OpenListClient (control plane)
- MetaStorePort (archive_map)
- OpQueue (task submission)
- OneBotApiPort (group notifications)
- CloudIngestService (bridge_in fallback)
- DownloadServerService (file source)
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from adapters.external.base import ExternalApiError, OpenListApiError
from adapters.external.openlist import OpenListClient
from core.domain.enums import BridgeTaskState
from core.log import logger
from ports.meta_store import MetaStorePort

if TYPE_CHECKING:
    from core.services.cloud_ingest import CloudIngestService
    from core.services.download_server import DownloadServerService
    from core.services.op_queue import OpQueue
    from ports.onebot_api import OneBotApiPort


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BridgeService:
    """OpenList bridge orchestration service.

    Handles:
    - bridge_out: Group file -> OpenList (offline download)
    - bridge_in: OpenList -> Group file (URL upload or fetch fallback)
    - Task polling and ledger management
    """

    def __init__(
        self,
        client: OpenListClient,
        store: MetaStorePort,
        config,
        queue: "OpQueue",
        api: "OneBotApiPort",
        ingest: "CloudIngestService",
        dlserver: "DownloadServerService",
    ):
        self._client = client
        self._store = store
        self._config = config
        self._queue = queue
        self._api = api
        self._ingest = ingest
        self._dlserver = dlserver

        # Polling state
        self._poll_task: asyncio.Task | None = None
        self._ledger_task: asyncio.Task | None = None
        self._stopping = False
        self._in_task_ids: set[str] = set()
        # REQ-14: cached URL-upload capability; None = not yet probed
        self._url_upload_capable: bool | None = None

        # Configuration
        self._interval = config.openlist_poll_interval_sec
        self._dst_dir = config.openlist_dst_dir
        self._dst_template = config.openlist_dst_dir_template
        self._min_bytes = config.bridge_min_bytes
        self._max_bytes = config.bridge_max_bytes

    # -- User entry points (queue async, REQ-08) --

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

    # -- OpDispatcher handler (kind must be registered in op_dispatch.py) --

    async def handle_bridge_out(self, op) -> None:
        """Handle bridge_out operation (seven steps)."""
        gid = op.target
        rid = op.payload["resource_id"]

        # Step 1: dlserver guard (REQ-16)
        if not (self._dlserver.enabled and self._dlserver.http_port > 0):
            return self._fail(op, "download_server_disabled")

        # Step 2: Size window
        res = await self._store.get_resource_detail(gid, rid)
        if res is None:
            return self._fail(op, "not_found")
        size = int(res.get("size") or 0)
        if not self._size_ok(size):
            return self._fail(op, "size_filtered")

        # Step 3: Idempotency + remote probe (REQ-07)
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

        # Step 5: Idempotent mkdir (REQ-07: 405 tolerance)
        await self._client.mkdir(remote_dir)

        # Step 6: File source direct link (REQ-01: only generate URL, no byte read)
        url = self._dlserver.download_url(gid, rid)

        # Step 7: Control plane submit + ledger (REQ-03 binding; REQ-06 no URL)
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

    async def handle_bridge_in(self, op) -> None:
        """Handle bridge_in operation (OpenList -> group)."""
        path = op.payload["path"]
        gid = op.target

        # Get direct link
        try:
            link = await self._client.get_raw_url(path)
        except ExternalApiError as e:
            return self._fail(op, f"get_link_failed: {e.message}")

        # REQ-14: Capability self-adaptation
        # Check if NapCat supports URL upload (capability key: upload_group_file@url)
        url_upload_supported = await self._probe_url_upload()

        if url_upload_supported:
            # Zero disk IO path: direct URL upload
            try:
                await self._api.upload_group_file(gid, link.url, _basename(path))
                await self._store.upsert_archive_map(
                    {
                        "resource_id": 0,  # No resource_id for bridge_in
                        "group_id": gid,
                        "task_id": "",
                        "remote_path": path,
                        "direction": "in",
                        "state": BridgeTaskState.DONE.value,
                        "updated_at": _now(),
                    }
                )
                self._publish(op, state="done", percent=100.0)
                logger.info(f"[bridge] bridge_in done (URL upload): {path} -> {gid}")
                return
            except Exception as e:
                # REQ-14/HL-16: runtime failure degrades the cached capability,
                # then falls through to the fetch path instead of failing the op
                self._url_upload_capable = False
                logger.warning(
                    f"[bridge] URL upload failed ({e}), degrading to fetch fallback"
                )
        # Fallback: delegate to ingest.submit_fetch (REQ-19: only disk IO exception)
        # Ledger consumer starts BEFORE submit: create_task enters the ready
        # queue ahead of the worker wakeup, so the listener is registered
        # before any completion event can fire (no missed-event race).
        self._ensure_ledger_task()
        try:
            fetch_tid = await self._ingest.submit_fetch(
                gid, link.url, name=_basename(path)
            )
            self._in_task_ids.add(fetch_tid)
            await self._store.upsert_archive_map(
                {
                    "resource_id": 0,
                    "group_id": gid,
                    "task_id": fetch_tid,
                    "remote_path": path,
                    "direction": "in",
                    "state": BridgeTaskState.PENDING.value,
                    "updated_at": _now(),
                }
            )
            self._publish(op, state="pending", percent=0.0)
            logger.info(
                f"[bridge] bridge_in submitted (fetch fallback): "
                f"{path} -> {gid}, task={fetch_tid}"
            )
        except Exception as e:
            return self._fail(op, f"submit_fetch_failed: {e}")

    # -- Background polling (task-driven, REQ-10/18) --

    def _ensure_ledger_task(self) -> None:
        """Lazy start the ledger consumer (task-driven, REQ-18).

        Runs only while tracked bridge_in fetch tasks exist; exits by itself
        when the tracked set drains and no pending in-rows remain.
        """
        if self._ledger_task is not None and not self._ledger_task.done():
            return
        self._ledger_task = asyncio.create_task(
            self._queue_ledger_task(), name="bridge-ledger"
        )
        logger.info("[bridge] ledger task started")

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
        """Stop poll and ledger tasks (terminate cleanup, HL-14)."""
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

    async def recover(self) -> None:
        """Startup recovery scan (B-class necessary auto).

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
                    from adapters.external.base import normalize_task_state

                    state = normalize_task_state(task.state)

                await self._store.update_archive_state(row, state)
                if state == BridgeTaskState.DONE.value:
                    # D1 fix: rename UUID filename to intended name
                    await self._maybe_rename_to_intended(row)
                    await self._notify_group(row, state)
                elif state == BridgeTaskState.FAILED.value:
                    await self._notify_group(row, state)
        except ExternalApiError as e:
            logger.warning(f"[bridge] recovery failed: {e.message}")

        # Zombie pending_in convergence (B-class necessary auto, REQ-18):
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
                # No unfinished tasks -> auto stop (REQ-18)
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
                        # Double list missing -> backoff probe (REQ-10)
                        n = backoff.get(row["task_id"], 0)
                        backoff[row["task_id"]] = n + 1
                        if n % 3:
                            continue  # 10s->30s->60s cap, skip
                        stat = await self._client.stat(row["remote_path"])
                        if not stat:
                            continue
                        state = "done"  # Remote exists but task gone -> done
                    else:
                        from adapters.external.base import normalize_task_state

                        state = normalize_task_state(task.state)

                    # D1 fix: Rename UUID filename to intended name on completion
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
        """Consume queue events for bridge_in ledger linkage (REQ-18 task-driven).

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

    # -- Status and control --

    async def status(self, task_id: str | None = None) -> dict:
        """Query task status (single task by id, or aggregate counters)."""
        if task_id:
            row = await self._store.get_archive_map_by_task(task_id)
            if row is None:
                return {"task_id": task_id, "state": BridgeTaskState.UNKNOWN.value}
            return {
                "task_id": task_id,
                "state": row.get("state", BridgeTaskState.UNKNOWN.value),
                "direction": row.get("direction", ""),
                "group_id": row.get("group_id", ""),
                "remote_path": row.get("remote_path", ""),
                "updated_at": row.get("updated_at", ""),
            }

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

    # -- Internal helpers --

    def _fail(self, op, reason: str) -> None:
        """Mark operation as failed."""
        logger.warning(f"[bridge] {op.kind} failed: {reason}")
        self._publish(op, state="failed", detail=reason)

    def _done(
        self, op, *, skipped: bool = False, remote_path: str = "", detail: str = ""
    ) -> None:
        """Mark operation as done."""
        if not detail:
            detail = "skipped (already archived)" if skipped else ""
        self._publish(op, state="done", percent=100.0, detail=detail)

    def _publish(
        self, op_or_row: object, state: str, percent: float = 0.0, detail: str = ""
    ) -> None:
        """Publish SSE event."""
        if hasattr(op_or_row, "kind"):
            kind = op_or_row.kind
            target = op_or_row.target
            task_id = op_or_row.id if hasattr(op_or_row, "id") else ""
        else:
            kind = f"bridge_{op_or_row.get('direction', 'out')}"
            target = op_or_row.get("group_id", "")
            task_id = op_or_row.get("task_id", "")

        self._queue.publish(
            {
                "type": "bridge",
                "kind": kind,
                "target": target,
                "task_id": task_id,
                "state": state,
                "percent": percent,
                "detail": detail,
                "ts": time.time(),
            }
        )

    async def _notify_group(self, row: dict, state: str) -> None:
        """Send group notification for completed tasks."""
        try:
            gid = row.get("group_id", "")
            rid = row.get("resource_id", 0)
            direction = row.get("direction", "out")
            remote_path = row.get("remote_path", "")

            if state == BridgeTaskState.DONE.value:
                if direction == "out":
                    msg = f"[Bridge] File archived: {remote_path}"
                else:
                    msg = f"[Bridge] File delivered to group: {_basename(remote_path)}"
            else:
                msg = f"[Bridge] Task failed: {remote_path}"

            # Use send_group_msg (cloud_ingest.py:124-127 pattern)
            await self._api.send_group_msg(
                gid, [{"type": "text", "data": {"text": msg}}]
            )
        except Exception as e:
            logger.warning(f"[bridge] group notification failed: {e}")

    def _size_ok(self, size: int) -> bool:
        """Check if file size is within configured bounds."""
        if self._min_bytes > 0 and size < self._min_bytes:
            return False
        if self._max_bytes > 0 and size > self._max_bytes:
            return False
        return True

    def _render_dst(
        self, dst_dir: str, group_id: str, filename: str
    ) -> tuple[str, str]:
        """Render destination path (literal replace, no str.format).

        Template example: {group_id}/{filename}
        - Replaces {group_id} with actual group ID
        - Replaces {filename} with actual filename
        - Combines with dst_dir base path

        Returns:
            (remote_dir, remote_path): directory and full file path
        """
        # Template: {group_id}/{filename} -> literal replace
        relative = self._dst_template
        relative = relative.replace("{group_id}", group_id)
        relative = relative.replace("{filename}", filename)

        # Normalize: remove leading/trailing slashes from relative
        relative = relative.strip("/")

        # Combine with dst_dir
        dst_base = dst_dir.rstrip("/")

        # remote_dir = dst_dir + relative directory part (without filename)
        if "/" in relative:
            dir_part = relative.rsplit("/", 1)[0]
            remote_dir = f"{dst_base}/{dir_part}"
        else:
            remote_dir = dst_base

        # remote_path = dst_dir + full relative path
        remote_path = f"{dst_base}/{relative}"

        # Normalize double slashes and ensure leading slash
        remote_dir = "/" + remote_dir.replace("//", "/").strip("/")
        remote_path = "/" + remote_path.replace("//", "/").strip("/")

        return remote_dir, remote_path

    async def _in_pending(self) -> bool:
        """Check if there are pending bridge_in tasks."""
        rows = await self._store.list_archive_map(
            states=(BridgeTaskState.PENDING.value, BridgeTaskState.RUNNING.value),
            direction="in",
        )
        return len(rows) > 0

    async def _maybe_rename_to_intended(self, row: dict) -> None:
        """Rename UUID filename to intended name after task completion (D1 fix).

        OpenList offline download names files from URL (often UUID).
        This method renames the file to the intended name stored in archive_map.
        Matches by file size to avoid renaming the wrong file.
        """
        remote_path = row.get("remote_path", "")
        if not remote_path:
            return

        # Extract intended filename from remote_path
        intended_name = (
            remote_path.rstrip("/").rsplit("/", 1)[-1] if "/" in remote_path else ""
        )
        if not intended_name:
            return

        # Get current directory listing to find the actual file
        dir_path = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "/"
        try:
            files = await self._client.list_dir(dir_path)

            # First check: if intended name already exists, we're done
            for f in files:
                if f.name == intended_name:
                    return  # Already correctly named

            # Second check: get the expected file size from resource detail
            resource = await self._store.get_resource_detail(
                row.get("group_id", ""), row.get("resource_id", 0)
            )
            expected_size = int(resource.get("size", 0)) if resource else 0

            # Find UUID-named files that match the expected size
            for f in files:
                if f.is_dir:
                    continue
                # Match by size (exact or within 1% tolerance for rounding)
                if expected_size > 0 and f.size > 0:
                    size_diff = abs(f.size - expected_size) / expected_size
                    if size_diff > 0.01:  # More than 1% difference
                        continue

                # Found a match - rename it
                try:
                    old_path = f"{dir_path}/{f.name}"
                    await self._client.rename(old_path, intended_name)
                    logger.info(
                        f"[bridge] renamed {f.name} -> {intended_name} (size={f.size})"
                    )
                    # Update remote_path in archive_map
                    new_remote_path = f"{dir_path}/{intended_name}"
                    await self._store.update_archive_remote_path(
                        row["resource_id"],
                        row["group_id"],
                        row["direction"],
                        new_remote_path,
                    )
                    row["remote_path"] = new_remote_path
                    return
                except Exception as e:
                    logger.debug(f"[bridge] rename attempt failed for {f.name}: {e}")
                    continue

            logger.warning(f"[bridge] no matching file found for rename in {dir_path}")
        except Exception as e:
            logger.warning(f"[bridge] rename check failed: {e}")

    async def _probe_url_upload(self) -> bool:
        """Probe if NapCat supports URL upload (REQ-14).

        Detection strategy:
        1. Check cached result from previous probe
        2. Check if upload_group_file is explicitly unsupported
        3. Default: assume supported (SnowLuma and modern NapCat support URL upload)
           - If URL upload fails at runtime, bridge_in falls back to fetch

        Result is cached after first successful detection.
        """
        # Return cached result if available
        if self._url_upload_capable is not None:
            return self._url_upload_capable

        try:
            # Only disable if explicitly marked unsupported
            upload_cap = self._api.capability("upload_group_file")
            if upload_cap.value == "unsupported":
                self._url_upload_capable = False
                logger.info(
                    "[bridge] URL upload capability: False (upload_group_file unsupported)"
                )
                return False

            # Default: assume supported (SnowLuma, modern NapCat accept URLs)
            # If the API doesn't actually support URL upload, the call will fail
            # and bridge_in will fall back to fetch (REQ-19)
            self._url_upload_capable = True
            logger.info("[bridge] URL upload capability: True (default/confirmed)")
            return True
        except Exception as e:
            logger.warning(f"[bridge] capability probe failed: {e}")
            self._url_upload_capable = True  # Optimistic: try URL first
            return True


def _basename(path: str) -> str:
    """Extract filename from path."""
    return path.rstrip("/").rsplit("/", 1)[-1] if "/" in path else path


def _split_ext(filename: str) -> tuple[str, str]:
    """Split filename into base and extension."""
    if "." in filename:
        idx = filename.rfind(".")
        return filename[:idx], filename[idx:]
    return filename, ""


def _short_suffix() -> str:
    """Generate short suffix for conflict resolution."""
    import random
    import string

    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
