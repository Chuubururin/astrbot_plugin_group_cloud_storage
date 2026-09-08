from __future__ import annotations

import asyncio
import time

from core.domain.sync import GroupInfo, ScanResult
from core.log import logger


class ScanMixin:
    """Scan orchestration methods."""

    async def _with_timeout(self, coro, timeout: float = 15.0):
        """Wrap an external call with a timeout so one hung group API cannot
        stall the whole scan round (surfaces as a TimeoutError)."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"call timeout after {timeout}s") from e

    async def scan_owned(
        self,
        include_capacity: bool = True,
        force_role_scan: bool = False,
        account_bot=None,
        api_override=None,
        group_filter: list[str] | None = None,
    ) -> ScanResult:
        """Scan group info (caller enqueues via OpQueue; per-group calls
        self-throttle).

        group_filter: after hash sharding, each bot scans only its own shard
        (None = all groups).

        Efficiency strategy:
        - Owned determination is an incremental group-list diff: only **new**
          groups call `get_group_member_info(self)` (single-member self-query,
          lightweight); cached groups keep their role instead of re-fetching
          the full member list per group
        - Capacity collection is full (when `include_capacity=True`, refreshed
          in the background for cross-group statistics)
        - Progress is pushed via queue.publish (i/N), visible on the Page SSE
        """
        api = api_override or self.api
        if account_bot is not None and hasattr(api, "with_bot"):
            api.with_bot(account_bot)
        me = (await api.get_login_info() or {}).get("user_id")
        me = str(me or "")
        # Register bot -> account_id mapping and restore this account's groups
        # to managed=1
        if me and self._on_account_resolved:
            try:
                await self._on_account_resolved(account_bot, me)
            except Exception as e:
                logger.debug(f"[group-scan] on_account_resolved callback failed: {e}")
        groups = await api.list_groups()
        # Hash-shard filter: groups in this shard plus DB-unknown new groups
        # (discovery critical path) always pass
        if group_filter is not None:
            filter_set = set(group_filter)
            known_ids = {g.group_id for g in await self.store.list_groups()}
            groups = [
                g for g in groups
                if str(g.get("group_id") or "") in filter_set
                or str(g.get("group_id") or "") not in known_ids
            ]
        group_total = len(groups)
        # Incremental judgment: read the existing role cache
        known = {g.group_id: g for g in await self.store.list_groups()}
        owned = 0
        failed = 0  # groups whose API calls failed (circuit breaker signal)
        now = int(time.time())
        judged = 0  # new groups actually judged in this run
        last_pub = 0.0
        for i, g in enumerate(groups, 1):
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            prev = known.get(gid)
            role = prev.role if prev else "unknown"
            # New group -> lightweight self-query to determine owned
            # (one API call per new group)
            if role in ("unknown",) or force_role_scan:
                try:
                    await self.queue.acquire()
                    me_info = await self._with_timeout(api.get_group_member_info(gid, me))
                    role = "owned" if me_info.get("role") == "owner" else "member"
                    judged += 1
                except Exception as e:
                    logger.warning(f"[group-scan] role judge failed for {gid}: {e}")
                    role = prev.role if prev else "unknown"
                    failed += 1
            # Capacity collection: an fs failure returns None -> keep the prev
            # values instead of overwriting with 0
            cap_used = cap_total = cap_count = cap_limit = 0
            cap_ok = False
            album_c = essence_c = 0
            if include_capacity:
                try:
                    await self.queue.acquire()
                    _cap = await self._with_timeout(
                        self._capacity_of(gid, api)
                    )
                    if _cap is not None:
                        cap_used, cap_total, cap_count, cap_limit = _cap
                        cap_ok = True
                except Exception as e:
                    logger.debug(f"[group-scan] fs_info unavailable for {gid}: {e}")
            if not cap_ok and prev:
                cap_used = prev.used_space
                cap_total = prev.total_space
                cap_count = prev.file_count
                cap_limit = prev.limit_count
            # Album/essence collection persisted as resources (full cadence,
            # every group)
            if include_capacity:
                try:
                    await self.queue.acquire()
                    albums_raw = await self._with_timeout(api.get_qun_album_list(gid))
                    album_c = len(albums_raw)
                    await self.queue.acquire()
                    essences_raw = await self._with_timeout(api.get_essence_msg_list(gid))
                    essence_c = len(essences_raw)
                    await self.store.upsert_album_essence(gid, albums_raw, essences_raw)
                except Exception as e:
                    logger.debug(
                        f"[group-scan] album/essence unavailable for {gid}: {e}"
                    )
            if role == "owned":
                owned += 1
            # Persist while scanning: lists are visible immediately during the
            # scan instead of one bulk write at the end
            await self.store.upsert_groups(
                [
                    GroupInfo(
                        group_id=gid,
                        group_name=str(g.get("group_name") or ""),
                        role=role,
                        last_scan_at=now,
                        used_space=cap_used,
                        total_space=cap_total,
                        file_count=cap_count,
                        limit_count=cap_limit,
                        album_count=album_c,
                        essence_count=essence_c,
                        account_id=me,
                    )
                ]
            )
            # Group-info TTL scheduling (scan_schedule): after a successful
            # collection, the upcoming rescan time is advanced so group info
            # (capacity/album/essence counts) stays fresh; due groups are
            # picked up for rescan by the lifecycle periodic loop
            # (ttl<=0 = disabled)
            if include_capacity and self.group_info_ttl_hours > 0:
                try:
                    await self.store.upsert_scan_schedule(
                        gid, int(time.time()) + int(self.group_info_ttl_hours * 3600)
                    )
                except Exception as e:
                    logger.debug(f"[group-scan] schedule upsert failed for {gid}: {e}")
            # Per-group chaining: hand this group to the file scanner right
            # away (role_determined = the group's role is now known; a new
            # group whose judge failed must not chain)
            if self.on_group_scanned is not None:
                try:
                    await self.on_group_scanned(
                        group_id=gid,
                        account_id=me,
                        file_count=cap_count,
                        album_count=album_c,
                        essence_count=essence_c,
                        is_new=prev is None,
                        role_determined=role != "unknown",
                    )
                except Exception as e:
                    logger.warning(
                        f"[group-scan] on_group_scanned callback failed for {gid}: {e}"
                    )
            if i % 10 == 0 or i == group_total or time.monotonic() - last_pub >= 5.0:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                        "judged": judged,
                    }
                )
                # Live refresh during scan: the frontend re-fetches partial
                # data with debounce (load while scanning)
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                    }
                )
                last_pub = time.monotonic()
        self.last_result = ScanResult(total=group_total, owned=owned, scanned_at=now, failed=failed)
        logger.info(
            f"[group-scan] done: total={group_total} owned={owned} "
            f"failed={failed} judged_new={judged} (ts={now})"
        )
        if self.auto_label:
            await self.auto_fill_labels()
        return self.last_result

    async def default_range_ids(self) -> list[str]:
        """Default range: the first group in sort order with no capacity data
        (used space unknown), plus up to 2 groups ranked above it (fewer at
        the list boundary); groups with unknown capacity are targeted first.
        Empty when every group has capacity data."""
        groups = [g for g in await self.store.list_groups() if getattr(g, "managed", 1)]
        ordered = sorted(groups, key=lambda g: (g.sort_order or 0, g.group_id))
        # Unknown capacity = no total, or used == 0 and never scanned
        # (last_scan_at missing)
        target_i = next(
            (
                i
                for i, g in enumerate(ordered)
                if g.total_space <= 0 or (g.used_space <= 0 and not g.last_scan_at)
            ),
            None,
        )
        if target_i is None:
            return []
        start = max(0, target_i - 2)
        return [g.group_id for g in ordered[start : target_i + 1]]

    async def scan_owned_incremental(
        self, account_bot=None, api_override=None,
        group_filter: list[str] | None = None,
        include_capacity: bool = True,
    ) -> ScanResult:
        """Incremental group info sync (default cadence):
        - Only new groups / groups with unknown capacity get capacity
          collection + owned determination
        - Already-known groups: only group_name/last_scan_at are updated
          (no capacity fetch)

        group_filter: after hash sharding, each bot scans only its own shard
        (None = all groups).
        """
        api = api_override or self.api
        if account_bot is not None and hasattr(api, "with_bot"):
            api.with_bot(account_bot)
        me = (await api.get_login_info() or {}).get("user_id")
        me = str(me or "")
        # Register bot -> account_id mapping and restore this account's groups
        # to managed=1
        if me and self._on_account_resolved:
            try:
                await self._on_account_resolved(account_bot, me)
            except Exception as e:
                logger.debug(f"[group-scan] on_account_resolved callback failed: {e}")
        groups = await api.list_groups()
        known = {g.group_id: g for g in await self.store.list_groups()}
        # Hash-shard filter: groups in this shard plus DB-unknown new groups
        # (discovery critical path) always pass
        if group_filter is not None:
            filter_set = set(group_filter)
            known_ids = set(known.keys())
            groups = [
                g for g in groups
                if str(g.get("group_id") or "") in filter_set
                or str(g.get("group_id") or "") not in known_ids
            ]
        group_total = len(groups)
        now = int(time.time())
        owned = 0
        failed = 0  # groups whose API calls failed (circuit breaker signal)
        judged = 0
        last_pub = 0.0
        for i, g in enumerate(groups, 1):
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            prev = known.get(gid)
            # New group or unknown capacity -> judge + collect capacity;
            # known group -> name/timestamp only
            need = prev is None or prev.total_space <= 0 or not prev.last_scan_at
            role = prev.role if prev else "unknown"
            album_c = essence_c = 0
            cap_used = cap_total = cap_count = cap_limit = 0
            cap_ok = False
            if need:
                if role in ("unknown",):
                    try:
                        await self.queue.acquire(mult=2.0)
                        me_info = await self._with_timeout(
                            api.get_group_member_info(gid, me)
                        )
                        role = "owned" if me_info.get("role") == "owner" else "member"
                        judged += 1
                    except Exception as e:
                        logger.warning(f"[group-scan] role judge failed for {gid}: {e}")
                        role = prev.role if prev else "unknown"
                        failed += 1
                try:
                    await self.queue.acquire(mult=2.0)
                    _cap = await self._with_timeout(
                        self._capacity_of(gid, api)
                    )
                    if _cap is not None:
                        cap_used, cap_total, cap_count, cap_limit = _cap
                        cap_ok = True
                except Exception as e:
                    logger.debug(f"[group-scan] fs_info unavailable for {gid}: {e}")
                if not cap_ok and prev:
                    cap_used = prev.used_space
                    cap_total = prev.total_space
                    cap_count = prev.file_count
                    cap_limit = prev.limit_count
                # Resource stats: album/essence (collected only for new or
                # unknown groups, persisted as resources)
                try:
                    await self.queue.acquire(mult=2.0)
                    albums_raw = await self._with_timeout(api.get_qun_album_list(gid))
                    album_c = len(albums_raw)
                    await self.queue.acquire(mult=2.0)
                    essences_raw = await self._with_timeout(api.get_essence_msg_list(gid))
                    essence_c = len(essences_raw)
                    await self.store.upsert_album_essence(gid, albums_raw, essences_raw)
                except Exception as e:
                    logger.debug(
                        f"[group-scan] album/essence unavailable for {gid}: {e}"
                    )
            else:
                # Known groups skip the cloud capacity fetch; keep prev values
                cap_used = prev.used_space
                cap_total = prev.total_space
                cap_count = prev.file_count
                cap_limit = prev.limit_count
            if role == "owned":
                owned += 1
            # Persist while scanning: lists are visible immediately during
            # the scan
            await self.store.upsert_groups(
                [
                    GroupInfo(
                        group_id=gid,
                        group_name=str(g.get("group_name") or ""),
                        role=role,
                        last_scan_at=now,
                        used_space=cap_used,
                        total_space=cap_total,
                        file_count=cap_count,
                        limit_count=cap_limit,
                        album_count=album_c if need else prev.album_count,
                        essence_count=essence_c if need else prev.essence_count,
                        account_id=me,
                    )
                ]
            )
            # Per-group chaining: hand this group to the file scanner right
            # away (known groups keep their previous role -> always chain)
            if self.on_group_scanned is not None:
                try:
                    await self.on_group_scanned(
                        group_id=gid,
                        account_id=me,
                        file_count=cap_count,
                        album_count=album_c if need else prev.album_count,
                        essence_count=essence_c if need else prev.essence_count,
                        is_new=prev is None,
                        role_determined=role != "unknown",
                    )
                except Exception as e:
                    logger.warning(
                        f"[group-scan] on_group_scanned callback failed for {gid}: {e}"
                    )
            if i % 10 == 0 or i == group_total or time.monotonic() - last_pub >= 5.0:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                        "judged": judged,
                    }
                )
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                    }
                )
                last_pub = time.monotonic()
        self.last_result = ScanResult(total=group_total, owned=owned, scanned_at=now, failed=failed)
        logger.info(
            f"[group-scan] incremental done: total={group_total} "
            f"owned={owned} failed={failed} touched={judged}"
        )
        return self.last_result

    async def auto_fill_labels(self) -> int:
        """Auto-labeling (runs after a scan), like Windows duplicate-file
        numbering: existing labels are kept; unlabeled groups receive the
        next unused label in current order (A/B/C... or 01/02...) to aid
        sorting."""
        groups = await self.store.list_groups()
        unlabeled = [g for g in groups if not g.label]
        if not unlabeled:
            return 0
        taken = {g.label for g in groups if g.label}
        ordered = sorted(
            unlabeled,
            key=lambda g: (g.sort_order or 0, g.group_id),
        )
        use_digits = len(groups) > 26
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        seq = 0
        filled = 0
        for g in ordered:
            lab = ""
            for _ in range(999):
                nxt = str(seq + 1).zfill(2) if use_digits else letters[seq % 26]
                seq += 1
                if nxt not in taken:
                    lab = nxt
                    break
            if not lab:
                continue
            taken.add(lab)
            await self.store.update_group_fields(g.group_id, label=lab)
            filled += 1
        if filled:
            logger.info(f"[group-scan] auto-labeled {filled} groups")
        return filled
