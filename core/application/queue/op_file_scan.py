"""File-level scan execution -- full and differential reconciliation loops.

Split out of op_dispatch: these two loops are the bulk of the queue's
wall-clock work, and each owns its own progress throttling, consecutive-
failure cooldown and tail cleanup.  Keeping them here leaves OpDispatcher
a routing table.

Both are reached from OpDispatcher._dispatch via ``self`` and rely on the
host's scan / sync / queue / services attributes plus ``_account_of``
(group -> owning account routing).
"""

from __future__ import annotations

import asyncio
import time

from core.log import logger
from core.opctx import account_scope


class FileScanMixin:
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
            # Explicit range: the submit path is gated, but the queue is the
            # last line of defense (payload may predate a config/offline
            # change) — drop groups that are no longer openable instead of
            # pulling their cloud files through a possibly offline account.
            mg = self.config.get("managed_groups", [])
            targets = []
            for gid in op.payload.get("groups") or []:
                try:
                    await self.scan.assert_group_openable(str(gid), mg)
                except ValueError:
                    logger.info(f"[file-scan] skip not-openable group {gid}")
                else:
                    targets.append(str(gid))
        total = len(targets)
        failed = 0
        last_fail: str | None = None
        consecutive = 0
        last_pub = 0.0
        last_log = 0.0
        try:
            for i, gid in enumerate(targets, 1):
                # The group is now being processed: release the chained-scan
                # dedupe entry so a later group scan can queue it again
                self._chained_file_scan_groups.discard(str(gid))
                # Cooperative checkpoint: cancel/interrupt and pause take effect
                # between groups
                await self.queue.pause_check(op)
                # Rate-limit before each per-group call (3x the base interval)
                await self.queue.acquire(mult=3.0)
                # Route this group's sync to the account that owns it
                with account_scope(await self._account_of(gid)):
                    lock = self.services.lock_for(gid)
                    result = await self.sync.run_full_sync(gid, lock)
                    await self.refresh_capacity(gid)
                now = time.monotonic()
                if not result.ok and result.error:
                    failed += 1
                    consecutive += 1
                    last_fail = last_fail or str(result.error)
                    logger.debug(f"[file-scan] {gid} failed: {result.error}")
                    if (failed % 20 == 0) or (now - last_log > 30):
                        logger.warning(
                            f"[file-scan] {failed}/{i} groups failed so far "
                            f"(e.g. {last_fail[:80]})"
                        )
                        last_log = now
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
        finally:
            # Cancel/error path: release the remaining chained-scan entries and
            # invalidate the group indexes + notify the frontend. Both used to
            # sit after this block, so a cancelled scan skipped them (stale
            # index; handle()'s _announce is skipped for file_scan).
            for gid in targets:
                self._chained_file_scan_groups.discard(str(gid))
                if self.services.searchkv is not None:
                    self.services.searchkv.mark_dirty(gid)
            self.queue.publish(
                {"type": "data_changed", "kind": "file_scan", "target": "*", "ts": time.time()}
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
        try:
            for i, gid in enumerate(targets, 1):
                await self.queue.pause_check(op)
                await self.queue.acquire(mult=3.0)
                with account_scope(await self._account_of(gid)):
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
        finally:
            # Cancel/pause path: same lesson as do_file_scan -- these two
            # used to sit after the loop, so an interrupted diff scan
            # skipped them (stale search index).  handle()'s _announce is
            # skipped for diff_file_scan too, so without this the frontend
            # got no refresh event at all on cancel.
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
