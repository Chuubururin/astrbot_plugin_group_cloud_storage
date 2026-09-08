from __future__ import annotations

from typing import Callable, Awaitable

from core.domain.sync import GroupInfo
from core.log import logger
from core.opctx import account_scope
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
        on_group_scanned: Callable[..., Awaitable[None]] | None = None,
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
        # Per-group chaining callback (keyword-only kwargs: group_id,
        # account_id, file_count, album_count, essence_count, is_new,
        # role_determined): invoked right after each group's info is
        # persisted so the file/album/essence scanners continue that group
        # without waiting for the whole group traversal. When None the
        # dispatcher falls back to the bulk initial file scan.
        self.on_group_scanned = on_group_scanned

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

        Wither semantics: groups bound to a currently offline account are
        excluded in both branches (data kept; the list recovers when the
        account reconnects). Groups without a recorded owner stay visible.
        """
        all_groups = await self.store.list_groups()
        # Known ids from the unfiltered set: offline-filtered groups must not
        # resurface below as synthetic whitelist placeholders.
        known_all = {g.group_id for g in all_groups}
        # None = callback not wired (online set unknown -> filter nothing);
        # set() = every known account offline -> drop all owner-bound groups.
        online = self._online_account_ids()
        if online is not None:
            all_groups = [
                g
                for g in all_groups
                if not getattr(g, "account_id", "")
                or g.account_id in online
            ]
        mg = set(managed_groups or [])
        if mg:
            # Whitelist first: explicitly listed groups are always manageable
            # (even when flagged managed=0) — but the wither filter above still
            # hides entries bound to an offline account.
            out = [
                g
                for g in all_groups
                if g.group_id in mg or (getattr(g, "managed", 1) and g.role == "owned")
            ]
            for gid in mg - known_all:
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

    # ---------- Group open gate (offline/dissolved protection) ----------

    async def group_open_state(self, group_id: str, managed_groups: list[str]) -> dict:
        """Open-gate state of a group: managed / account-online / seen.

        Returns {managed, account_online, seen}; ``managed`` keeps the
        whitelist-union-owned semantics of is_page_managed (unknown groups
        count as unmanaged), ``account_online`` is False when the group is
        bound to an account that is not currently online, and ``seen`` is
        False when the group is not in the local table (dissolved or never
        scanned).
        """
        gid = str(group_id or "")
        row: GroupInfo | None = None
        for g in await self.store.list_groups(include_hidden=True):
            if g.group_id == gid:
                row = g
                break
        if row is None:
            return {"managed": False, "account_online": False, "seen": False}
        managed = await self.is_page_managed(gid, managed_groups)
        # Wither semantics: a group bound to an offline account is not
        # openable even when the whitelist still lists it. Groups without a
        # recorded owner stay open (unknown-owner legacy data).
        account_online = True
        if managed and getattr(row, "account_id", ""):
            online = self._online_account_ids()
            if online is not None:
                account_online = row.account_id in online
        return {
            "managed": managed,
            "account_online": account_online,
            "seen": bool(row.last_scan_at or row.group_name or row.role != "unknown"),
        }

    async def assert_group_openable(self, group_id: str, managed_groups: list[str], online_ids=None) -> None:
        """Raise ValueError when the group must not be opened on the Page.

        Conditions (whisper semantics, data kept):
        - not page-managed (not in whitelist/owned) -> "群不在受管范围";
        - owning account offline -> "群归属账号离线，暂不可操作";
        - dissolved (owner account is online but the group no longer appears
          in its group list) -> "群已解散或不可访问".
        The dissolved check is a cheap remote verify: only executed when the
        local row claims the group is bound to the *online* set and passed
        the managed check — i.e. the offline case never triggers an API call.
        """
        state = await self.group_open_state(group_id, managed_groups)
        if not state["managed"]:
            raise ValueError("group not managed")
        if not state["account_online"]:
            raise ValueError("群归属账号离线，暂不可操作")
        # Dissolved verify: ask the owning account's bot; a group that has
        # been dissolved fails get_group_info on NapCat/Lagrange-style
        # implementations (empty reply or error). Failure here is treated as
        # dissolved (fail-closed): an unreachable bot must not keep the gate
        # open. Unknown-capability adapters that always answer are unaffected.
        gid = str(group_id or "")
        row = await self._group_row(gid)
        owner = str(getattr(row, "account_id", "") or "") if row else ""
        # The remote verify must run under the owning account: without the
        # scope it lands on best_bot, which is usually not a member of this
        # group, and the empty reply misreads as "dissolved".
        with account_scope(owner):
            info: dict = {}
            try:
                info = await self.api.get_group_info(gid, no_cache=True) or {}
            except Exception:
                info = {}
        if not str((info or {}).get("group_name") or ""):
            raise ValueError("群已解散或不可访问")

    async def _group_row(self, group_id: str):
        """Local groups-table row for the gate (include hidden)."""
        gid = str(group_id or "")
        for g in await self.store.list_groups(include_hidden=True):
            if g.group_id == gid:
                return g
        return None

    def _online_account_ids(self) -> set[str] | None:
        """Online account-id set from the runtime callback; None when the
        callback is not wired (unknown -> treat every account as online)."""
        cb = getattr(self, "_get_online_ids", None)
        return cb() if cb is not None else None

    def set_online_ids_callback(self, cb: Callable[[], set[str]]) -> None:
        """Wire the runtime online-account callback (main.py)."""
        self._get_online_ids = cb
