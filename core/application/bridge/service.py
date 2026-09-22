"""BridgeService — composition root with __init__ and shared helpers."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from adapters.external.openlist import OpenListClient
from core.domain.enums import BridgeTaskState
from core.log import logger
from ports.meta_store import MetaStorePort
from core.application.bridge import _basename

from .submit import SubmitMixin
from .inbound import InboundMixin
from .polling import PollingMixin
from .recovery import RecoveryMixin

if TYPE_CHECKING:
    from core.application.ingest import CloudIngestService
    from core.application.download_server import DownloadServerService
    from core.application.queue import OpQueue
    from ports.onebot_api import OneBotApiPort


class BridgeService(SubmitMixin, InboundMixin, PollingMixin, RecoveryMixin):
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
        self._poll_task = None
        self._ledger_task = None
        self._sweep_task = None
        self._stopping = False
        self._in_task_ids: set[str] = set()
        # Cached URL-upload capability; None = not yet probed
        self._url_upload_capable: bool | None = None

        # Configuration (sizes resolve via string-unit keys with legacy
        # byte-key fallback — see PluginConfig)
        self._interval = config.openlist_poll_interval_sec
        self._dst_dir = config.openlist_dst_dir
        self._dst_template = config.openlist_dst_dir_template
        self._min_bytes = config.bridge_min_bytes
        self._max_bytes = config.bridge_max_bytes

    # -- Public client passthrough --
    # The webapi layer calls these instead of reaching into _client, so the
    # OpenList client stays an internal detail of the bridge service.

    @property
    def poll_interval(self) -> float:
        """Configured auto-poll interval (0 = manual mode)."""
        return self._interval

    async def submit_offline_download(
        self,
        urls: list[str],
        path: str,
        *,
        tool: str = "SimpleHttp",
        delete_policy: str = "delete_on_upload_succeed",
    ) -> list:
        return await self._client.submit_offline_download(
            urls, path, tool=tool, delete_policy=delete_policy
        )

    async def submit_offline(self, url: str, group_id: str, filename: str) -> str:
        """Offline-download one remote URL into the per-group archive dir.

        Renders ``openlist_dst_dir`` + the {group_id}/{filename} template
        (same source as bridge_out), mkdirs idempotently, then submits and
        returns the task id ("" when OpenList reports none). Callers pass a
        final URL only — naming/proxy concerns of the source URL belong to
        the caller, the storage-layout contract lives here.
        """
        remote_dir, _remote_path = self._render_dst(
            self._dst_dir or "/", group_id, filename
        )
        await self._client.mkdir(remote_dir)
        tasks = await self._client.submit_offline_download([url], remote_dir)
        return tasks[0].id if tasks else ""

    async def mkdir(self, path: str) -> None:
        await self._client.mkdir(path)

    async def get_raw_url(self, path: str):
        return await self._client.get_raw_url(path)

    async def rename(self, path: str, new_name: str) -> None:
        await self._client.rename(path, new_name)

    async def remove(self, dir_path: str, names: list[str]) -> None:
        await self._client.remove(dir_path, names)

    async def move(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        await self._client.move(src_dir, dst_dir, names)

    async def copy(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        await self._client.copy(src_dir, dst_dir, names)

    async def remove_empty_dirs(self, src_dir: str, names: list[str]) -> None:
        await self._client.remove_empty_dirs(src_dir, names)

    async def recursive_move(
        self, src_dir: str, dst_dir: str, names: list[str]
    ) -> None:
        await self._client.recursive_move(src_dir, dst_dir, names)

    async def verify_copy(
        self, src_dir: str, dst_dir: str, names: list[str]
    ) -> list[dict]:
        """Post-copy size verification (advisory; never raises).

        OpenList copies are fire-and-forget from our side: a same-storage
        copy finishes immediately, a cross-storage copy runs as a background
        task. Size comparison is the cheap first-pass integrity check (the
        rclone convention): it cannot detect bit rot, but it does catch
        silent truncation — 2026-09-12 live: OpenList v4.2.5 same-mount copy
        truncated a 20 MiB file to exactly 16 MiB while reporting success.
        Statuses: ok / mismatch (with sizes) / pending (dst not visible yet,
        copy task may still be running) / skipped (src gone or stat error).
        """
        results: list[dict] = []
        for name in names:
            try:
                src = await self._client.stat(f"{src_dir.rstrip('/')}/{name}")
                dst = await self._client.stat(f"{dst_dir.rstrip('/')}/{name}")
            except Exception:
                results.append({"name": name, "status": "skipped"})
                continue
            if src is None:
                results.append({"name": name, "status": "skipped"})
                continue
            if dst is None:
                results.append({"name": name, "status": "pending"})
                continue
            if src.is_dir or dst.is_dir:
                results.append({"name": name, "status": "ok"})
                continue
            if src.size == dst.size:
                results.append({"name": name, "status": "ok"})
            else:
                results.append(
                    {
                        "name": name,
                        "status": "mismatch",
                        "src_size": src.size,
                        "dst_size": dst.size,
                    }
                )
        return results

    # -- Startup preflight --

    async def preflight_dst(self) -> bool:
        """Check that ``openlist_dst_dir`` lives under an OpenList mount.

        Walks the destination root's ancestor chain and reports the first
        prefix that no mount owns.  The plugin cannot fix the configuration,
        but it can move the failure from "first user transfer" (where the
        queue classifies it as a retriable IO error, backs off three times
        and then fails permanently - a config error never heals) to "plugin
        load", with a log that says what to change.

        Read-only: only ``fs/get`` probes, nothing is created.  Returns True
        when the chain is addressable, False otherwise.  Never raises.
        """
        dst = (self._dst_dir or "").strip().rstrip("/")
        if not dst:
            return True
        parts = [seg for seg in dst.split("/") if seg]
        # Shallowest first: the first prefix no mount owns *is* the
        # misconfiguration, and stopping there keeps the healthy case at a
        # single probe (a mounted root answers for its whole subtree).
        for depth in range(1, len(parts) + 1):
            prefix = "/" + "/".join(parts[:depth])
            try:
                ok, reason = await self._client.probe_mount(prefix)
            except Exception as e:
                logger.warning(f"[bridge] preflight probe {prefix!r} failed: {e}")
                return True
            if not ok:
                logger.error(
                    f"[bridge] preflight FAILED: openlist_dst_dir {dst!r} is not "
                    f"under any OpenList mount - {prefix!r} reports {reason!r}. "
                    f"Every bridge_out transfer will fail until this is fixed "
                    f"(OpenList's mkdir cannot create a mount point). Fix: create "
                    f"a storage in OpenList whose mount_path is a parent of "
                    f"{dst!r}, or point openlist_dst_dir at an existing mount "
                    f"(e.g. /smb/bridge-test)."
                )
                return False
            # A mount owns this prefix, so the whole subtree is addressable
            # (mkdir creates the remaining directories inside it).
            logger.info(
                f"[bridge] preflight ok: openlist_dst_dir {dst!r} resolves under "
                f"mount prefix {prefix!r}"
            )
            return True

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
            # Op carries the correlation id in task_id (never .id) -- the
            # frontend matches SSE events against queued task ids.
            task_id = op_or_row.task_id
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
            direction = row.get("direction", "out")
            remote_path = row.get("remote_path", "")

            if state == BridgeTaskState.DONE.value:
                if direction == "out":
                    msg = f"[Bridge] File archived: {remote_path}"
                else:
                    msg = f"[Bridge] File delivered to group: {_basename(remote_path)}"
            else:
                msg = f"[Bridge] Task failed: {remote_path}"

            # Notify via send_group_msg
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

    async def _maybe_rename_to_intended(self, row: dict) -> None:
        """Rename the UUID filename to the intended name after task
        completion.

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
                # Match by size (exact or within 1% tolerance for rounding).
                # Unknown size on either side -> skip: without this guard the
                # loop renames an arbitrary file in the directory (e.g. after
                # the resource row was deleted and expected_size is 0).
                if expected_size <= 0 or f.size <= 0:
                    continue
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
