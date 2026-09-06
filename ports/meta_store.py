"""MetaStorePort — the single persistence exit point.

The command layer must not touch the database directly; all access goes
through this port.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.domain.sync import (
    GroupInfo,
    VolumeInfo,
    Page,
    ResourceQuery,
    ResourceStats,
    SyncLog,
    SyncResult,
)


@runtime_checkable
class MetaStorePort(Protocol):
    """Metadata persistence abstraction (SqliteMetaStore; a Protocol like the limiter port)."""

    async def upsert_resources(self, items: list) -> int:
        """Idempotent UPSERT; returns the number of written/updated rows."""

    async def query_resources(self, q: ResourceQuery) -> Page:
        """Paged resource query."""

    async def get_resource_detail(self, group_id: str, id: int) -> dict | None:
        """Resource detail (scoped to one group to prevent cross-group ID leaks)."""

    async def update_resource_fields(self, id: int, **fields) -> None:
        """Update resource fields (name/folder_id/status; column whitelist; after admin ops)."""

    async def stats(self, group_id: str) -> ResourceStats:
        """Aggregate statistics for one group."""

    async def list_groups(self) -> list[GroupInfo]:
        """Group list (includes role/display_name/sort_order/label)."""

    async def upsert_groups(self, items: list[GroupInfo]) -> int:
        """Group info upsert (written by scan results; group_id unique key)."""

    async def update_group_fields(self, group_id: str, **fields) -> None:
        """Update group admin fields (display_name/label; column name whitelist)."""

    async def get_resource_any(self, id: int) -> dict | None:
        """Fetch a resource by primary key across groups (fallback lookup)."""

    async def count_active(self, group_id: str) -> int:
        """Count of active files in a group (fallback for capacity persistence)."""

    async def upsert_album_essence(
        self, group_id: str, albums: list, essences: list, account_id: str = ""
    ) -> None:
        """Store albums and essence messages as resources in the unified catalog (summary only)."""

    async def upsert_folders(self, group_id: str, folders: list[dict]) -> None:
        """Persist folder entities (idempotent on folder_id/folder_name/parent_id)."""

    async def list_folders_detail(self, group_id: str) -> list[dict]:
        """List folder entities for a group (folder tree)."""

    async def clear_folders(self, group_id: str) -> None:
        """Clear a group's folders (called before a full refresh)."""

    async def sum_resource_sizes(self, group_id: str) -> int:
        """Used capacity (exact index-based total): sum of active file sizes in the group."""

    async def set_groups_managed(self, group_ids: list[str], managed: int) -> None:
        """Batch-set the managed flag (0=removed from managed list; scans will not restore it)."""

    async def reorder_groups(self, ordered_ids: list[str]) -> None:
        """Persist sort_order following the given order."""

    async def get_resource_by_resource_id(self, resource_id: str) -> dict | None:
        """Fetch a resource by its unique key (for volume parents / backfill)."""

    async def insert_volumes(self, items: list[VolumeInfo]) -> None:
        """Register volumes (seq unique within a parent resource)."""

    async def list_volumes(self, parent_resource_id: str) -> list[VolumeInfo]:
        """Return a parent resource's volumes in sequence order."""

    async def update_volume_fields(
        self, parent_resource_id: str, seq: int, **fields
    ) -> None:
        """Update volume fields (source_ref/busid/sha256/status; whitelist check)."""

    async def backfill_volume_by_part(
        self, group_id: str, part_name: str, source_ref: str, busid: int
    ) -> int:
        """Event-driven backfill: match the group's unready volumes by part name; returns count."""

    async def remove_volumes(self, parent_resource_id: str) -> None:
        """Delete all volumes of a parent resource (cascade cleanup)."""

    async def mark_missing_as_deleted(
        self, group_id: str, complete: bool, source_file_ids: set[str]
    ) -> int:
        """Orphan cleanup: runs only when complete=True; returns rows marked deleted."""

    async def create_sync_log(self, log: SyncLog) -> int:
        """Create a sync log entry; returns the log id."""

    async def finish_sync_log(self, log_id: int, result: SyncResult) -> None:
        """Finalize a sync log entry."""

    async def save_snapshot(self, snap) -> None:
        """Save a statistics snapshot (append only)."""

    async def fts_match(
        self, group_id: str | None, q: str, limit: int = 2000
    ) -> list[int]:
        """Disk-backed full-text search."""

    async def mark_all_groups_managed(self, managed: int) -> int:
        """Batch-set the managed flag on all groups (0 at startup, restored after scan)."""

    async def mark_account_groups_managed(self, account_id: str, managed: int) -> int:
        """Set group managed flags by account (0=hidden after account offline, 1=restore)."""

    async def restore_account_groups(self, account_id: str) -> int:
        """Restore a back-online account's groups to managed=1; returns the
        number of rows actually flipped (0 = account was never hidden)."""

    async def list_account_group_ids(self, account_id: str) -> list[str]:
        """Group ids bound to an account (post-offline full rescan input)."""

    # ---------- archive_map  ----------

    async def get_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> dict | None:
        """Get archive map entry for a specific resource and direction."""

    async def upsert_archive_map(self, row: dict) -> None:
        """Insert or update archive map entry."""

    async def clear_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> None:
        """Remove archive map entry for a specific resource and direction."""

    async def list_archive_map(
        self, *, states: tuple[str, ...], direction: str
    ) -> list[dict]:
        """List archive map entries filtered by state and direction."""

    async def update_archive_state(self, row: dict, state: str) -> None:
        """Update state of an archive map entry."""

    async def list_archived_done_ids(
        self, resource_ids: list[int], direction: str = "out"
    ) -> set[int]:
        """Return the ids among the given resource ids whose archive is done (state=done).

        Supports the "in netdisk" file status filter and batch checks in
        list projections.
        """

    async def update_archive_state_by_task(self, task_id: str, state: str) -> None:
        """Update state of an archive map entry by task_id."""

    async def update_archive_remote_path(
        self, resource_id: int, group_id: str, direction: str, new_remote_path: str
    ) -> None:
        """Update remote_path of an archive map entry (for rename operations)."""

    async def get_archive_map_by_task(self, task_id: str) -> dict | None:
        """Get archive map entry by bridge task id (OpenList task or fetch op id)."""

    # ---------- netdisk_meta (netdisk index and tags) ----------

    async def upsert_netdisk_rows(self, rows: list[dict]) -> int:
        """Register browsed rows: idempotent INSERT OR IGNORE; keeps manual tags; returns count."""

    async def get_netdisk_meta(self, dir_prefix: str) -> list[dict]:
        """Return tagged rows by directory prefix (remote_path LIKE dir_prefix%)."""

    async def set_netdisk_tags(self, remote_path: str, tags: str) -> None:
        """Set tags for a single file (overwrites)."""

    async def mark_netdisk_indexed(self, remote_paths: list[str]) -> None:
        """Backfill indexed_at after deep indexing."""

    # ---------- Task ledger and operation log ----------

    async def ledger_upsert(
        self,
        task_id: str,
        kind: str,
        target: str = "",
        payload: dict | None = None,
        state: str = "pending",
        retries: int = 0,
        error: str | None = None,
    ) -> None:
        """Task ledger upsert (states: pending/running/paused/retry/done/failed/cancelled)."""

    async def ledger_get(self, task_id: str) -> dict | None:
        """Fetch a task ledger entry by task_id."""

    async def ledger_query(
        self,
        state: str | None = None,
        kind: str | None = None,
        target: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Paged task ledger query (ordered by updated_at desc)."""

    async def ledger_reconcile(self) -> int:
        """Startup reconciliation: whitelist kinds become pending (resumable), others failed."""

    async def ops_append(
        self, task_id: str, action: str, before: dict | None, after: dict | None
    ) -> None:
        """Append an operation log entry (before/after snapshots for reversible ops).

        Direct operations pass task_id=''.
        """

    async def ops_list(self, task_id: str) -> list[dict]:
        """List operation log entries for a task (ordered by seq asc)."""

    async def ops_last_for_resource(self, action: str, resource_id: int) -> dict | None:
        """Locate direct operations: latest operation log entry for a resource (e.g. tag undo)."""

    async def hide_account_groups(self, account_id: str, hidden: int) -> int:
        """Account offline: hide all its groups (hidden=1, not deleted); back online: set to 0."""

    async def upsert_scan_schedule(
        self, group_id: str, next_scan_at: int, priority: int = 0
    ) -> None:
        """Group info TTL scheduling (scan_schedule): record the next due scan time."""

    async def list_due_scan_groups(
        self, now: int | None = None, limit: int = 100
    ) -> list[dict]:
        """Groups due for rescan (TTL claim); higher priority and earlier due first."""

    async def init(self) -> None:
        """Create tables / run migrations."""

    async def close(self) -> None: ...
