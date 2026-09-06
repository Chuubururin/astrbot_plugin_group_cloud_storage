from __future__ import annotations

from typing import Callable, Awaitable

from core.domain.sync import GroupInfo
from core.log import logger
from core.application.queue import OpQueue
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

from .capacity import CapacityMixin
from core.domain.sync import ScanResult
from .scan import ScanMixin


class GroupScanService(ScanMixin, CapacityMixin):
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        auto_label: bool = True,
        on_account_resolved: Callable[[object, str], Awaitable[None]] | None = None,
        group_info_ttl_hours: float = 24.0,
    ):
        self.api = api
        self.store = store
        self.queue = queue
        self.auto_label = auto_label
        self.last_result: ScanResult | None = None
        # Callback after a successful scan: (bot, account_id) -> register the
        # mapping + restore managed
        self._on_account_resolved = on_account_resolved
        # Group-info TTL (scan_schedule): the next due time rolls forward
        # after each collection; due groups are picked up for rescan by the
        # lifecycle periodic loop (0 = TTL rescan disabled)
        self.group_info_ttl_hours = float(group_info_ttl_hours)

    async def run_batch_ops(self, op) -> None:
        """Batch group ops: rename / join option / remark, real per-group
        API calls plus local backfill."""
        action = op.payload.get("action")
        value = op.payload.get("value")
        group_ids = list(op.payload.get("group_ids") or [])
        total = len(group_ids)
        if action == "rename":
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_name(gid, str(value))
                await self.store.update_group_fields(gid, group_name=str(value))
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"改名 {gid}",
                    }
                )
        elif action == "add_option":
            add_type = int(value)
            if add_type not in (1, 2, 3, 4, 5):
                raise ValueError("add_type must be 1..5")
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_add_option(gid, add_type)
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"加群方式 {gid}",
                    }
                )
        elif action == "remark":
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_remark(gid, str(value))
                await self.store.update_group_fields(gid, display_name=str(value))
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"备注 {gid}",
                    }
                )
        else:
            raise ValueError(f"unknown batch action: {action}")
        logger.info(f"[group-scan] batch {action} done: {total} groups")

    async def rename_remote(
        self,
        group_id: str,
        name: str,
        display_name: str | None = None,
        label: str | None = None,
    ) -> None:
        """Real rename (set_group_name, owner permission; runs inside
        OpQueue).

        Verify-then-backfill: after the API succeeds, the group name is read
        back; local display_name/label are written only when it matches
        (no backfill without verification).
        """
        from core.domain.enums import OneBotApiError, OneBotErrorKind

        await self.api.set_group_name(group_id, name)
        info = await self.api.get_group_info(group_id)
        actual = str((info or {}).get("group_name") or "")
        if actual != name:
            raise OneBotApiError(
                OneBotErrorKind.REMOTE_ERROR,
                "set_group_name",
                f"verify mismatch: got {actual!r} want {name!r}",
            )
        fields: dict = {}
        if display_name:
            fields["display_name"] = display_name
        if label is not None:
            fields["label"] = label
        if fields:
            await self.store.update_group_fields(group_id, **fields)

    async def list_page_groups(self, managed_groups: list[str]) -> list[GroupInfo]:
        """List of groups manageable from the Page.

        Rule: non-empty whitelist -> whitelist entries are always manageable
        (including groups flagged managed=0), unioned with owned groups
        (groups I created); empty whitelist -> all groups are manageable
        (except removed groups with managed=0).
        """
        all_groups = await self.store.list_groups()
        mg = set(managed_groups or [])
        if mg:
            # Whitelist first: explicitly listed groups are always manageable
            # (even when flagged managed=0)
            out = [
                g
                for g in all_groups
                if g.group_id in mg or (getattr(g, "managed", 1) and g.role == "owned")
            ]
            known = {g.group_id for g in all_groups}
            for gid in mg - known:
                out.append(GroupInfo(group_id=gid, role="unknown"))
            return out
        # Empty whitelist: allow all (except removed groups with managed=0)
        return [g for g in all_groups if getattr(g, "managed", 1)]

    async def is_page_managed(self, group_id: str, managed_groups: list[str]) -> bool:
        """Page manageability check (empty whitelist -> all groups allowed)."""
        if not managed_groups:
            return True
        return any(
            g.group_id == group_id for g in await self.list_page_groups(managed_groups)
        )
