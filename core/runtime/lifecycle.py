"""Runtime lifecycle — init, terminate, reset/rebuild."""
from __future__ import annotations

import asyncio
import time

from core.log import logger


class LifecycleManager:
    """Manages plugin lifecycle: init, terminate, reset/rebuild."""

    def __init__(
        self,
        kernel,
        *,
        resolver,
        store,
        queue,
        dlserver,
        bridge=None,
        openlist_client=None,
        config,
        auto_scan_hours=0,
        platform_bots_ref: list | None = None,
    ):
        self.kernel = kernel
        self._resolver = resolver
        self.store = store
        self.queue = queue
        self.dlserver = dlserver
        self.bridge = bridge
        self.openlist_client = openlist_client
        self.config = config
        self.auto_scan_hours = auto_scan_hours

        self._inited = False
        self._init_lock = asyncio.Lock()
        self._scan_submitted = False
        self._tasks: set[asyncio.Task] = set()
        self._auto_scan_task: asyncio.Task | None = None
        self._resolve_task: asyncio.Task | None = None
        self._periodic_resolve_task: asyncio.Task | None = None
        self._platform_bot = None
        self._platform_bots: list = platform_bots_ref if platform_bots_ref is not None else []

    def _create_runtime_task(self, coro, *, name: str):
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self.kernel is not None:
            self.kernel.track_task(task)
        return task

    async def init(self) -> None:
        if self._inited:
            return
        async with self._init_lock:
            if not self._inited:
                await self.store.init()
                await self.store.upsert_resources([])
                await self.kernel.services.task_control.reconcile()
                await self.queue.start()
                self._inited = True
                await self.dlserver.start()
                await self.resolve_platform_bot()
                await self.maybe_submit_scan()
                if (
                    self._periodic_resolve_task is None
                    or self._periodic_resolve_task.done()
                ):
                    self._periodic_resolve_task = self._create_runtime_task(
                        self._periodic_resolve_and_scan(),
                        name="periodic-bot-resolve",
                    )
                if self.bridge is not None:
                    self._create_runtime_task(
                        self.bridge.recover(), name="bridge-recover"
                    )
                logger.info("[group_cloud_storage] runtime initialized")

    async def resolve_platform_bot(self) -> None:
        """Resolve the aiocqhttp platform bot proactively (logic lives in
        PlatformBotResolver).

        On failure, starts a background retry (covers bots that reconnect over
        reverse WebSocket after the plugin is ready); stops automatically once
        found. The event path (_bot_scope) always serves as a fallback.
        """
        try:
            await self._resolver.resolve_once()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] platform bot resolve failed: {e}")
        self._platform_bot = self._resolver.preferred_bot
        self._platform_bots.clear()
        self._platform_bots.extend(self._resolver.bots)
        if not self._platform_bots and (
            self._resolve_task is None or self._resolve_task.done()
        ):
            self._resolve_task = self._create_runtime_task(
                self._resolver.ensure(interval_sec=30.0, max_attempts=20),
                name="platform-bot-resolve",
            )

    async def maybe_submit_scan(self) -> None:
        """Lazily start the incremental scan once a platform or event bot is ready."""
        if self._scan_submitted:
            return
        if not self._resolver.bots:
            logger.debug(
                "[group_cloud_storage] initial scan deferred: no bot available"
            )
            return
        self._scan_submitted = True
        await self.queue.start()
        await self.queue.submit("scan", target="*", payload={"mode": "incremental"})
        logger.info("[group_cloud_storage] initial group scan queued")
        if self.auto_scan_hours > 0 and (
            self._auto_scan_task is None or self._auto_scan_task.done()
        ):
            self._auto_scan_task = self._create_runtime_task(
                self.auto_scan_loop(), name="auto-scan"
            )

    async def _periodic_resolve_and_scan(self) -> None:
        """Periodically re-resolve platform bots: dynamic bot discovery plus
        offline detection with data cleanup.

        Re-resolves every 60 seconds:
        1. Offline bots detected -> mark their groups managed=0 (data kept but hidden)
        2. New bots found -> trigger an incremental scan
        """
        last_due_check = 0.0  # throttles the group-info TTL claim (once every 10 min)
        while True:
            await asyncio.sleep(60)
            try:
                # 1. Detect offline bots and clean up their data
                stale_account_ids = await self._resolver.purge_stale_bots()
                for account_id in stale_account_ids:
                    n = await self.store.mark_account_groups_managed(account_id, 0)
                    if n:
                        logger.info(
                            f"[group_cloud_storage] account {account_id} "
                            f"offline: {n} groups hidden (managed=0)"
                        )
                self._platform_bots.clear()
                self._platform_bots.extend(self._resolver.bots)

                # 2. Detect new bots
                old_bots = set(id(b) for b in self._resolver.bots)
                await self._resolver.resolve_once()
                self._platform_bots.clear()
                self._platform_bots.extend(self._resolver.bots)
                new_bots = [b for b in self._resolver.bots if id(b) not in old_bots]
                if new_bots:
                    logger.info(
                        f"[group_cloud_storage] dynamic bot discovery: "
                        f"{len(new_bots)} new bot(s), total {len(self._resolver.bots)}"
                    )
                    # Trigger a scan for the new bots
                    await self.queue.submit(
                        "scan", target="*", payload={"mode": "incremental"}
                    )

                # 3. Group-info TTL rescan: every 10 minutes, claim groups whose
                #    scan_schedule expired (up to 50 per cycle to avoid bursts)
                #    and submit a full rescan so group info stays fresh.
                #    scan_owned rolls the next due time forward after a
                #    successful collection, forming a continuous refresh loop.
                if time.monotonic() - last_due_check >= 600:
                    last_due_check = time.monotonic()
                    due = await self.store.list_due_scan_groups(limit=50)
                    if due:
                        ids = [str(d.get("group_id") or "") for d in due]
                        ids = [g for g in ids if g]
                        if ids:
                            logger.info(
                                "[group_cloud_storage] group info ttl expired: "
                                f"rescanning {len(ids)} group(s)"
                            )
                            await self.queue.submit(
                                "scan",
                                target="*",
                                payload={"mode": "full", "group_ids": ids},
                            )
            except Exception as e:
                logger.debug(f"[group_cloud_storage] periodic resolve failed: {e}")

    async def auto_scan_loop(self) -> None:
        """Periodic differential reconciliation of the netdisk.

        Submits diff_file_scan every interval hours (directory-level listing of
        the root and first-level folders; entries that vanished from the cloud
        are removed from the ledger immediately, while entries merely absent
        from a listing are frozen instead of deleted). Full scans remain a
        manual exception (files/scan mode=all/range, controlled from the task
        Tab).
        """
        while True:
            await asyncio.sleep(self.auto_scan_hours * 3600)
            try:
                await self.queue.submit(
                    "diff_file_scan", target="*", payload={"mode": "diff"}
                )
                logger.info("[group_cloud_storage] periodic diff scan queued")
            except Exception as e:
                logger.warning(f"[group_cloud_storage] diff scan submit failed: {e}")

    async def terminate(self) -> None:
        """Called on plugin disable/reload: cleans up auto scan, OpQueue and SQLite."""
        if self.kernel is not None:
            await self.kernel.cancel_all()
        if self._resolve_task:
            self._resolve_task.cancel()
        if self._periodic_resolve_task:
            self._periodic_resolve_task.cancel()
        try:
            await self.dlserver.shutdown()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] dlserver shutdown failed: {e}")
        if self._auto_scan_task:
            self._auto_scan_task.cancel()
        if self.bridge is not None:
            try:
                await self.bridge.stop_polling()
            except Exception as e:
                logger.warning(f"[group_cloud_storage] bridge stop failed: {e}")
        if self.openlist_client is not None:
            try:
                await self.openlist_client.aclose()
            except Exception as e:
                logger.warning(
                    f"[group_cloud_storage] openlist client close failed: {e}"
                )
        try:
            await self.queue.shutdown()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] queue shutdown failed: {e}")
        try:
            await self.store.close()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] close store failed: {e}")
        logger.info("OneBot Resource Manager terminated.")
