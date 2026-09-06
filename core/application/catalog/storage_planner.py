"""StoragePlanner — cross-group storage group-selection strategy and capacity alerts.

Constraints: ~10GB capacity per group and at most 300 groups per QQ account —
upload target groups are selected automatically to make full use of the
multi-group space, with unified statistics and alerts.
"""

from __future__ import annotations

from core.domain.sync import GroupInfo
from ports.meta_store import MetaStorePort

# Alert thresholds (used/total)
ALERT_YELLOW = 0.90
ALERT_RED = 0.98


class StoragePlanner:
    def __init__(self, store: MetaStorePort):
        self.store = store

    async def pick_group(
        self,
        candidates: list[GroupInfo],
        requested_bytes: int = 0,
        prefer_owned: bool = True,
    ) -> GroupInfo | None:
        """Pick a group (ordering: owned first, then sort_order/group_id).

        Capacity precheck: groups whose free space (total-used) is below
        requested_bytes are skipped automatically; when all owned candidates
        are skipped, the full pool is searched (overflow switching onto member
        groups); if no group has enough space, the first group in order is
        returned.
        """
        pool = candidates if candidates else []
        if not pool:
            return None
        need = max(requested_bytes, 1)
        # Sort once and reuse: candidates are usually small, but this path is
        # the hot path of every upload.
        ordered = sorted(pool, key=lambda g: (g.sort_order or 0, g.group_id))

        def _first_usable(groups):
            return next(
                (
                    g
                    for g in groups
                    if g.total_space <= 0
                    or (g.total_space - g.used_space) >= need
                ),
                None,
            )

        primary = ordered
        if prefer_owned:
            owned = [g for g in ordered if g.role == "owned"]
            if owned:
                primary = owned
        # Preferred: the first usable group in the owned ordered pool
        hit = _first_usable(primary)
        if hit is not None:
            return hit
        # Owned groups all insufficient -> expand to the full pool (overflow
        # switching: falls onto member groups)
        hit = _first_usable(ordered)
        return hit if hit is not None else ordered[0]

    async def pick_min_group_id(self, candidates: list[GroupInfo]) -> GroupInfo | None:
        """The group with the smallest group id (default target for album/essence
        uploads — the quota is unknown, so no capacity precheck is done and
        selection is purely by smallest group id; owned/sort_order preferences
        are ignored)."""
        pool = candidates if candidates else []
        if not pool:
            return None
        return sorted(pool, key=lambda g: g.group_id)[0]

    async def pick_min_group_for_size(
        self, candidates: list[GroupInfo], requested_bytes: int = 0
    ) -> GroupInfo | None:
        """Default for group-file uploads — the smallest group id whose free
        space is greater than the file to upload.

        Candidates are scanned in ascending group-id order; the first group
        whose free space (total-used) >= requested_bytes is recommended; when
        all fall short, the group with the most free space is returned
        (overflow semantics, consistent with pick_group; the caller surfaces
        it explicitly).
        """
        pool = candidates if candidates else []
        if not pool:
            return None
        need = max(requested_bytes, 1)

        def _usable(groups):
            return [
                g
                for g in groups
                if g.total_space <= 0 or (g.total_space - g.used_space) >= need
            ]

        ordered = sorted(pool, key=lambda g: g.group_id)
        hit = _usable(ordered)
        if hit:
            return hit[0]
        # All insufficient -> group with the most free space (overflow
        # semantics; the frontend can state the shortage explicitly)
        return max(pool, key=lambda g: (g.total_space - g.used_space), default=None)

    @staticmethod
    def capacity_state(used: int, total: int) -> str:
        """Capacity state: ok / warn (>=90%) / danger (>=98%)."""
        if total <= 0:
            return "unknown"
        ratio = used / total
        if ratio >= ALERT_RED:
            return "danger"
        if ratio >= ALERT_YELLOW:
            return "warn"
        return "ok"

    @staticmethod
    def capacity_stats(groups: list[GroupInfo]) -> dict:
        """Unified multi-group statistics: total capacity/used/group count/alerting groups."""
        total = sum(g.total_space for g in groups if g.total_space > 0)
        used = sum(g.used_space for g in groups if g.used_space > 0)
        alerts = [
            {
                "group_id": g.group_id,
                "shown_name": g.shown_name,
                "state": StoragePlanner.capacity_state(g.used_space, g.total_space),
                "pct": round((g.used_space / g.total_space) * 100, 1)
                if g.total_space
                else 0,
            }
            for g in groups
            if g.total_space > 0
            and StoragePlanner.capacity_state(g.used_space, g.total_space) != "ok"
        ]
        return {
            "groups": len(groups),
            "total_space": total,
            "used_space": used,
            "free_space": total - used,
            "alerts": alerts,
        }
