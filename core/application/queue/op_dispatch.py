"""OpDispatcher -- operation dispatch for OpQueue.

The host keeps only a thin _op_handler shell that delegates here; dispatch of
scan/file_scan/sync/file operations/essence/transfer/batch and other kinds,
capacity integration, and scan progress throttling all live in this module,
testable independently of the Star host.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any, Callable

from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger
from .health import HealthCircuitBreaker
from .capacity import CapacityMixin


class OpDispatcher(CapacityMixin):
    def __init__(
        self,
        services: object,
        api: object,
        store: object,
        sync: object,
        scan: object,
        ingest: object | None,
        transfer: object | None,
        ops: object,
        queue: object,
        config: dict,
        bots_getter: Callable[..., Any],
        bridge: object | None = None,
        bot_api_factory: Callable[[Any, float], Any] | None = None,
    ):
        self.services = services
        self.api = api
        self.store = store
        self.sync = sync
        self.scan = scan
        self.ingest = ingest
        self.transfer = transfer
        self.ops = ops
        self.queue = queue
        self.config = config
        self.bridge = bridge
        self._bots_getter = bots_getter
        # bot -> dedicated OneBot adapter factory (multi-account parallel
        # scanning); injected by runtime assembly so the application layer does
        # not import a concrete adapter.
        self._bot_api_factory = bot_api_factory
        # Parallel scan semaphore (configurable cap, default 8, to limit
        # stacked rate-control pressure)
        max_concurrent = int(config.get("max_concurrent_scans", 8))
        max_concurrent = max(1, min(max_concurrent, 20))
        self._scan_semaphore = asyncio.Semaphore(max_concurrent)
        # Per-account circuit breaker (independent health score per bot)
        self._health = HealthCircuitBreaker()
        # Compatibility aliases retained for callers/tests that inspect state.
        self._bot_health = self._health.bot_health
        self._global_cooldown_until = 0.0
        logger.info(f"[op-queue] scan concurrency: {max_concurrent}")

    # -- Group hash sharding -------------------------------------------
    def _assign_groups(
        self, all_groups: list[dict], bots: list
    ) -> dict[int, list[str]]:
        """Stably shard the group list across accounts via
        hash(group_id) % len(bots).

        Returns {bot_index: [group_id, ...]}. When an account goes offline its
        shard migrates naturally (hash recomputation, no explicit migration
        table).
        """
        if not bots:
            return {}
        n = len(bots)
        assignment: dict[int, list[str]] = {i: [] for i in range(n)}
        for g in all_groups:
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            shard = int(hashlib.md5(gid.encode()).hexdigest(), 16) % n
            assignment[shard].append(gid)
        return assignment

    def _bot_id(self, bot) -> str:
        """Extract a stable bot identifier (used as the health map key)."""
        return str(getattr(bot, "_uin", None) or id(bot))

    def _check_global_circuit_breaker(self, bots: list) -> bool:
        """Check the global circuit breaker.

        Returns True when more than half the accounts are cooling down (all
        scans should pause).
        """
        now = time.monotonic()
        if now < self._global_cooldown_until:
            return True
        if not bots:
            return False
        cooling_count = sum(
            1 for b in bots
            if self._health.health_for(self._bot_id(b)).is_cooling
        )
        if cooling_count > len(bots) / 2:
            pause = 600  # 10 minutes
            self._global_cooldown_until = now + pause
            logger.warning(
                f"[circuit-breaker] GLOBAL PAUSE {pause}s — "
                f"{cooling_count}/{len(bots)} bots in cooldown (IP-level throttle?)"
            )
            return True
        return False

    async def _run_scan_for_bot(
        self, bot, mode: str, group_filter: list[str] | None = None
    ) -> None:
        """Run a scan for one bot with a dedicated adapter (no global state
        contention).
        """
        if self._bot_api_factory is None:
            raise RuntimeError(
                "OpDispatcher 缺少 bot_api_factory 注入（多账号扫描不可用）"
            )

        bot_id = self._bot_id(bot)
        health = self._health.health_for(bot_id)

        # Circuit breaker: skip while this account is cooling down
        if health.is_cooling:
            logger.debug(f"[circuit-breaker] skipping bot {bot_id} (cooling)")
            return

        try:
            interval = float(self.config.get("request_interval_ms", 1000)) / 1000.0
        except (TypeError, ValueError):
            interval = 1.0
        # +-20% CSPRNG jitter so accounts do not hit server-side aggregated
        # rate control in phase
        jitter = interval * (0.8 + 0.4 * secrets.randbelow(1001) / 1000.0)
        bot_api = self._bot_api_factory(bot, jitter)
        async with self._scan_semaphore:
            try:
                if mode == "incremental":
                    result = await self.scan.scan_owned_incremental(
                        account_bot=bot, api_override=bot_api,
                        group_filter=group_filter,
                    )
                else:
                    result = await self.scan.scan_owned(
                        account_bot=bot, api_override=bot_api,
                        group_filter=group_filter,
                    )
                # Circuit breaker signal: failure ratio >50% counts as a
                # failure for this bot
                total = getattr(result, "total", 0) or 0
                failed = getattr(result, "failed", 0) or 0
                if total > 0 and failed > total / 2:
                    health.record_failure()
                    logger.warning(
                        f"[circuit-breaker] bot {bot_id}: "
                        f"{failed}/{total} groups failed (>50%)"
                    )
                else:
                    health.record_success()
            except Exception as e:
                health.record_failure()
                logger.warning(
                    f"[op-queue] scan bot {bot_id} failed: {e}"
                )

    async def handle(self, op) -> None:
        if op.kind == "scan":
            bots = self._bots_getter() or [None]
            mode = op.payload.get("mode")
            group_filter = op.payload.get("group_ids")
            if len(bots) <= 1:
                # Single bot or none: scan directly on one account (zero overhead)
                b = bots[0] if bots else None
                if mode == "incremental":
                    await self.scan.scan_owned_incremental(account_bot=b, group_filter=group_filter)
                else:
                    await self.scan.scan_owned(account_bot=b, group_filter=group_filter)
            else:
                # Global circuit breaker check
                if self._check_global_circuit_breaker(bots):
                    logger.warning("[op-queue] scan skipped: global circuit breaker")
                    return
                # Stable sharding: sort by bot id to remove iteration-order
                # nondeterminism
                bots_sorted = sorted(bots, key=lambda b: self._bot_id(b))
                # Group hash sharding: union the group lists from each bot
                # API (key path for discovering new groups)
                all_group_ids: set[str] = set()
                for b in bots_sorted:
                    try:
                        _api = self._bot_api_factory(b, 0.1)
                        raw = await _api.list_groups()
                        for g in raw:
                            gid = str(g.get("group_id") or "")
                            if gid:
                                all_group_ids.add(gid)
                    except Exception as e:
                        logger.debug(f"[op-queue] pre-scan list_groups failed for {self._bot_id(b)}: {e}")
                all_groups = [{"group_id": gid} for gid in all_group_ids]
                if group_filter:
                    wanted = {str(g) for g in group_filter}
                    all_groups = [g for g in all_groups if g["group_id"] in wanted]
                assignment = self._assign_groups(all_groups, bots_sorted)
                # Parallel across bots (dedicated adapter per bot + sharded
                # group lists)
                tasks = [
                    asyncio.create_task(
                        self._run_scan_for_bot(b, mode, assignment.get(i, [])),
                        name=f"scan-{self._bot_id(b)}",
                    )
                    for i, b in enumerate(bots_sorted)
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        elif op.kind == "file_scan":
            await self.do_file_scan(op)
        elif op.kind == "diff_file_scan":
            # Withering differential reconciliation (root + first-level folder
            # listings; groups absent from the listing are frozen, not
            # withered; full scans remain a manual exception)
            await self.do_diff_scan(op)
        elif op.kind == "rename":
            await self.scan.rename_remote(
                op.target,
                op.payload["name"],
                display_name=op.payload.get("display_name"),
                label=op.payload.get("label"),
            )
        elif op.kind == "sync_all":
            raise ValueError("sync_all removed: use files/scan (all/range)")
        elif op.kind == "sync":
            lock = self.services.lock_for(op.target)
            result = await self.sync.run_full_sync(op.target, lock)
            await self.refresh_capacity(op.target)  # refresh capacity stats after file sync
            if not result.ok and result.error:
                raise RuntimeError(result.error)
        elif op.kind in (
            "upload",
            "delete",
            "move_file",
            "replace_name",
            "convert_volumes",
        ):
            await self.run_file_op_and_announce(op)
        elif op.kind == "create_folder":
            await self.ops.handle(op)
        elif op.kind in (
            "essence_save",
            "essence_delete",
            "fetch",
            "video_upload",
            "video_album",
            "image_album",  # import a single image into the group album
        ):
            await self.ingest.handle(op)
        elif op.kind == "netdisk_index":
            # Deep indexing: manual task, rate-limited at directory
            # granularity and cancellable
            if self.services.netdisk is None:
                raise OneBotApiError(
                    OneBotErrorKind.LOCAL_ERROR,
                    op.kind,
                    "netdisk service not configured",
                )
            await self.services.netdisk.handle_index(op)
        elif op.kind in ("bridge_out", "bridge_in"):
            if self.bridge is None:
                raise OneBotApiError(
                    OneBotErrorKind.LOCAL_ERROR,
                    op.kind,
                    "bridge service not configured",
                )
            if op.kind == "bridge_out":
                await self.bridge.handle_bridge_out(op)
            else:
                await self.bridge.handle_bridge_in(op)
        elif op.kind == "batch_groups":
            await self.scan.run_batch_ops(op)
        else:
            # Unknown kind is a programming error: LOCAL_ERROR, not retried
            # (retrying cannot fix it)
            raise OneBotApiError(
                OneBotErrorKind.LOCAL_ERROR, op.kind, f"unknown op kind: {op.kind}"
            )

    async def run_file_op_and_announce(self, op) -> None:
        """After a file operation: incremental capacity write-back + KV
        maintenance + data_changed push.
        """
        try:
            await self.ops.handle(op)
            # After delete/rename-reupload: refresh the whole group's file
            # listing (mark_missing_as_deleted) so the local view strictly
            # matches the cloud (no stale index rows left active)
            if op.kind in ("delete", "replace_name"):
                lock = self.services.lock_for(op.target)
                await self.sync.run_full_sync(op.target, lock)
            await self.refresh_capacity(op.target)  # incremental capacity write on completion
        finally:
            if self.services.searchkv is not None and op.kind in (
                "upload",
                "delete",
                "move_file",
            ):
                self.services.searchkv.mark_dirty(op.target)
            self.queue.publish(
                {
                    "type": "data_changed",
                    "kind": op.kind,
                    "target": op.target,
                    "ts": time.time(),
                }
            )

    async def do_file_scan(self, op) -> None:
        """File scan across groups (all/range): per-group full_sync plus
        capacity refresh; progress is published live.
        """
        if op.payload.get("mode") == "all":
            groups = await self.scan.list_page_groups(
                self.config.get("managed_groups", [])
            )
            targets = [g.group_id for g in groups]
        else:
            targets = op.payload.get("groups") or []
        total = len(targets)
        failed = 0
        last_fail: str | None = None
        consecutive = 0
        last_pub = 0.0
        last_log = 0.0
        for i, gid in enumerate(targets, 1):
            # Cooperative checkpoint: cancel/interrupt and pause take effect
            # between groups
            await self.queue.pause_check(op)
            # Rate-limit before each per-group call (3x the base interval)
            await self.queue.acquire(mult=3.0)
            lock = self.services.lock_for(gid)
            result = await self.sync.run_full_sync(gid, lock)
            await self.refresh_capacity(gid)
            # Built-in auto op: sweep for over-threshold files that still live
            # as a single cloud file and queue their volume conversion (the
            # former user-facing "convert" button is gone). Never fails the scan.
            ops = getattr(self.services, "ops", None)
            if result.ok and ops is not None:
                try:
                    converted = await ops.sweep_convert_volumes(gid)
                    if converted:
                        logger.info(
                            f"[file-scan] {gid} auto convert_volumes x{converted}"
                        )
                except Exception as e:
                    logger.warning(f"[file-scan] {gid} convert sweep skipped: {e}")
            now = time.monotonic()
            if not result.ok and result.error:
                failed += 1
                consecutive += 1
                last_fail = last_fail or str(result.error)
                # Bulk remote failures log at debug; a warning summary every
                # 20 groups
                logger.debug(f"[file-scan] {gid} failed: {result.error}")
                if (failed % 20 == 0) or (now - last_log > 30):
                    logger.warning(
                        f"[file-scan] {failed}/{i} groups failed so far "
                        f"(e.g. {last_fail[:80]})"
                    )
                    last_log = now
                # Rate-control cooldown: 10 consecutive failures -> cool down
                # 60s (QQ rate-limit recovery window)
                if consecutive >= 10:
                    logger.warning(
                        f"[file-scan] {consecutive} consecutive failures; "
                        f"cooling 60s (risk control)"
                    )
                    await asyncio.sleep(60)
                    consecutive = 0
                    last_log = now
            else:
                consecutive = 0
            # Progress publish throttling (>=2s interval or every 10 groups)
            if (now - last_pub >= 2.0) or (i % 10 == 0):
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "file_scan",
                        "target": gid,
                        "i": i,
                        "n": total,
                        "detail": f"群 {gid}",
                    }
                )
                # Refresh while scanning: file lists and capacity become
                # visible as the scan progresses
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "file_scan",
                        "target": "*" if op.payload.get("mode") == "all" else gid,
                        "i": i,
                        "n": total,
                    }
                )
                last_pub = now
        # Scan complete: invalidate affected group indexes (lazy rebuild) +
        # dynamic refresh event
        if self.services.searchkv is not None:
            for gid in targets:
                self.services.searchkv.mark_dirty(gid)
        self.queue.publish(
            {
                "type": "data_changed",
                "kind": "file_scan",
                "target": "*",
                "ts": time.time(),
            }
        )
        logger.info(
            f"[file-scan] done: {total} groups (mode={op.payload.get('mode')}, "
            f"failed={failed})"
        )

    async def do_diff_scan(self, op) -> None:
        """Withering differential reconciliation.

        - Targets: all managed groups (target="*") or the given group
          (op.target);
        - each group runs run_diff_sync (root + first-level folder listing,
          directory level);
        - absent (complete=False) -> freeze the group (do not wither); 10
          consecutive failures trigger a 60s cooldown;
        - cooperative checkpoints (pause_check) keep scheduled scans governed
          by the task Tab;
        - full scans do not use this path (files/scan all remains manual).
        """
        if op.target and op.target != "*":
            targets = [str(op.target)]
        else:
            groups = await self.scan.list_page_groups(
                self.config.get("managed_groups", [])
            )
            targets = [g.group_id for g in groups]
        total = len(targets)
        failed = 0
        withered = 0
        consecutive = 0
        last_fail: str | None = None
        last_pub = 0.0
        for i, gid in enumerate(targets, 1):
            await self.queue.pause_check(op)
            await self.queue.acquire(mult=3.0)
            lock = self.services.lock_for(gid)
            result = await self.sync.run_diff_sync(gid, lock)
            now = time.monotonic()
            if not result.ok:
                failed += 1
                consecutive += 1
                last_fail = last_fail or str(result.error)
                if consecutive >= 10:
                    logger.warning(
                        f"[diff-scan] {consecutive} consecutive frozen groups; "
                        f"cooling 60s (risk control)"
                    )
                    await asyncio.sleep(60)
                    consecutive = 0
            else:
                consecutive = 0
                withered += result.files_removed
                # Refresh capacity after a successful differential pass
                # (lightweight, index-fallback figures)
                try:
                    await self.refresh_capacity(gid)
                except Exception as e:
                    logger.debug(f"[diff-scan] capacity refresh failed for {gid}: {e}")
            if (now - last_pub >= 2.0) or (i % 10 == 0) or i == total:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "diff_file_scan",
                        "target": gid,
                        "i": i,
                        "n": total,
                        "detail": f"差分 {gid}",
                    }
                )
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "diff_file_scan",
                        "target": "*",
                        "i": i,
                        "n": total,
                    }
                )
                last_pub = now
        if self.services.searchkv is not None:
            for gid in targets:
                self.services.searchkv.mark_dirty(gid)
        self.queue.publish(
            {
                "type": "data_changed",
                "kind": "diff_file_scan",
                "target": "*",
                "ts": time.time(),
            }
        )
        logger.info(
            f"[diff-scan] done: {total} groups (failed={failed}, withered={withered})"
        )
