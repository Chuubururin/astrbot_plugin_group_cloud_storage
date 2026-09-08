"""InboundMixin — inbound transfer methods (netdisk → group files)."""
from __future__ import annotations

from adapters.external.base import ExternalApiError
from core.domain.enums import BridgeTaskState
from core.log import logger
from core.application.bridge import _now, _basename

class InboundMixin:
    """Handle bridge_in operations (OpenList -> group)."""

    async def handle_bridge_in(self, op) -> None:
        """Handle bridge_in operation (OpenList -> group)."""
        path = op.payload["path"]
        gid = op.target

        # Get direct link
        try:
            link = await self._client.get_raw_url(path)
        except ExternalApiError as e:
            return self._fail(op, f"get_link_failed: {e.message}")

        # Capability self-adaptation
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
                # runtime failure degrades the cached capability,
                # then falls through to the fetch path instead of failing the op
                self._url_upload_capable = False
                logger.warning(
                    f"[bridge] URL upload failed ({e}), degrading to fetch fallback"
                )
        # Fallback: delegate to ingest.submit_fetch
        # BUG-24: check file size before committing to a full download.
        # Without this, a10GB netdisk file would be downloaded to local
        # disk before re-uploading, wasting time and space.
        try:
            stat = await self._client.stat(path)
            if stat and hasattr(stat, "size") and stat.size:
                if not self._size_ok(stat.size):
                    return self._fail(
                        op,
                        f"file too large: {stat.size} bytes exceeds configured limits",
                    )
        except Exception:
            pass  # stat failure is non-fatal; proceed with fetch
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

    async def _probe_url_upload(self) -> bool:
        """Probe if NapCat supports URL upload .

        Detection strategy:
        1. Check the cached probe result
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
            # and bridge_in will fall back to fetch
            self._url_upload_capable = True
            logger.info("[bridge] URL upload capability: True (default/confirmed)")
            return True
        except Exception as e:
            logger.warning(f"[bridge] capability probe failed: {e}")
            self._url_upload_capable = True  # Optimistic: try URL first
            return True
