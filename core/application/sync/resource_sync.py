"""ResourceSyncService - collection orchestration + consistency.

- Full sync: root + recursive subfolders -> batch idempotent UPSERT ->
  orphan cleanup (gated on complete) -> capacity -> stats -> manual
  snapshot -> sync log
- Event indexing: group_upload -> event-guaranteed fields persisted
- Same-group mutual exclusion: the caller passes an asyncio.Lock
- Failure: never crashes; SyncResult records completeness and errors
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from core.log import logger

from core.domain.enums import ResourceType, SyncKind, SyncStatus
from core.domain.resource import GroupFileList, Resource
from core.domain.sync import Snapshot, SyncLog, SyncResult
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort


class SyncAborted(Exception):
    """Full sync aborted because traversal failed (complete=False)."""


class ResourceSyncService:
    def __init__(self, api: OneBotApiPort, store: MetaStorePort):
        self.api = api
        self.store = store
        self._member_names: dict[str, str] = {}
        # Member lists change infrequently; keep a per-group cache for this
        # service instance so full and diff syncs do not repeat the same read.
        self._member_names_cache: dict[str, dict[str, str]] = {}
        self._member_names_inflight: dict[str, asyncio.Task[dict[str, str]]] = {}
        self._traverse_concurrency = 4

    # ---------- Uploader name resolution ----------

    async def _load_member_names(self, group_id: str) -> dict[str, str]:
        """Load the group member nickname map once per service instance (best effort)."""
        cached = self._member_names_cache.get(group_id)
        if cached is not None:
            return dict(cached)
        # Coalesce simultaneous full/diff requests without exposing mutable state.
        task = self._member_names_inflight.get(group_id)
        if task is None:
            task = asyncio.create_task(self._fetch_member_names(group_id))
            self._member_names_inflight[group_id] = task
        try:
            names = await task
            self._member_names_cache[group_id] = names
            return dict(names)
        finally:
            if task.done():
                self._member_names_inflight.pop(group_id, None)

    async def _fetch_member_names(self, group_id: str) -> dict[str, str]:
        try:
            members = await self.api.list_group_members(group_id)
        except Exception as e:  # a name resolution failure must not block
            logger.warning(
                f"[group_cloud_storage] member resolve failed for {group_id}: {e}"
            )
            return {}
        return {m.user_id: m.nickname for m in members if m.user_id}

    def _apply_names(self, group_id: str, items: list[Resource], member_names: dict[str, str] | None = None) -> None:
        names = member_names if member_names is not None else self._member_names
        for r in items:
            if r.uploader_name is None and r.uploader_id:
                r.uploader_name = names.get(r.uploader_id)

    # ---------- Full sync ----------

    async def run_full_sync(self, group_id: str, lock: asyncio.Lock) -> SyncResult:
        """Run one mutex-protected full sync (returns SyncResult; raises no
        business errors)."""
        if lock.locked():
            return SyncResult(
                status=SyncStatus.FAILED,
                error="same group sync already running",
            )
        async with lock:
            return await self._sync_unlocked(group_id)

    async def _sync_unlocked(self, group_id: str) -> SyncResult:
        log_id = await self.store.create_sync_log(
            SyncLog(group_id=group_id, kind=SyncKind.FULL, start_at=int(time.time()))
        )
        try:
            member_names = await self._load_member_names(group_id)
            files, folders, complete, error = await self._traverse(group_id)
            found = len(files)
            if not complete:
                raise SyncAborted(error or "traversal incomplete")

            # Folder persistence: rebuild the group's folder entities on each
            # full refresh
            await self.store.clear_folders(group_id)
            await self.store.upsert_folders(group_id, folders)

            resources = [Resource.from_group_file(group_id, f) for f in files]
            self._apply_names(group_id, resources, member_names)
            indexed = await self.store.upsert_resources(resources)

            # Orphan cleanup (only when complete=True)
            source_ids = {f.file_id for f in files}
            await self.store.mark_missing_as_deleted(group_id, True, source_ids)

            # Capacity + stats + manual snapshot
            fs_info = None
            try:
                fs_info = await self.api.get_group_fs_info(group_id)
            except Exception as e:
                logger.warning(
                    f"[group_cloud_storage] fs_info failed for {group_id}: {e}"
                )

            stats = await self.store.stats(group_id)
            if fs_info is not None:
                stats.used_space = fs_info.used_space
                stats.total_space = fs_info.total_space
                stats.limit_count = fs_info.limit_count
                await self.store.save_snapshot(
                    Snapshot(
                        group_id=group_id,
                        type=ResourceType.FILE.value,
                        file_count=stats.file_count,
                        total_size=stats.total_size,
                        used_space=stats.used_space,
                        total_space=stats.total_space,
                        detail={
                            "by_folder": stats.by_folder,
                            "by_uploader": stats.by_uploader,
                            "recent_7d": stats.recent_7d,
                        },
                        taken_at=int(time.time()),
                    )
                )

            result = SyncResult(
                status=SyncStatus.OK,
                files_found=found,
                files_indexed=indexed,
                complete=True,
            )
            logger.info(
                f"[group_cloud_storage] full sync {group_id}: found={found} indexed={indexed}"
            )
            return result
        except asyncio.CancelledError:
            await self.store.finish_sync_log(
                log_id,
                SyncResult(
                    status=SyncStatus.CANCELLED, complete=False, error="cancelled"
                ),
            )
            raise
        except SyncAborted as e:
            await self.store.finish_sync_log(
                log_id,
                SyncResult(status=SyncStatus.FAILED, complete=False, error=str(e)),
            )
            return SyncResult(status=SyncStatus.FAILED, complete=False, error=str(e))
        except Exception as e:
            logger.exception(f"[group_cloud_storage] full sync failed for {group_id}")
            await self.store.finish_sync_log(
                log_id,
                SyncResult(status=SyncStatus.FAILED, complete=False, error=str(e)),
            )
            return SyncResult(status=SyncStatus.FAILED, complete=False, error=str(e))

    async def _traverse(self, group_id: str) -> tuple[list, list, bool, str | None]:
        """BFS traversal of the folder tree; returns (files, folders, complete, error)."""
        files: list = []
        folders_seen: list = []
        folders_name: dict[str, str] = {}
        folders_parent: dict[str, str] = {}
        # Current folder for the abort log; the root listing failure (e.g. an
        # AUTHORITY_FAIL group) aborts before the loop ever binds it.
        folder_id: str | None = None
        queue: deque[tuple[str | None, str]] = deque([(None, "")])  # (folder_id, parent_id)
        try:
            # Read the root first, then process each BFS frontier concurrently.
            # A bounded frontier avoids unbounded task creation and preserves the
            # existing fail-closed semantics (any listing failure aborts traversal).
            seen: set[str] = set()
            while queue:
                frontier = [queue.popleft() for _ in range(len(queue))]

                async def read(entry: tuple[str | None, str]) -> tuple[tuple[str | None, str], GroupFileList]:
                    folder_id, _ = entry
                    result = (
                        await self.api.list_group_root(group_id)
                        if folder_id is None
                        else await self.api.list_group_folder(group_id, folder_id)
                    )
                    return entry, result

                for start in range(0, len(frontier), self._traverse_concurrency):
                    batch = frontier[start : start + self._traverse_concurrency]
                    results = await asyncio.gather(*(read(item) for item in batch))
                    for (folder_id, p_id), result in results:
                        if folder_id is not None:
                            for f in result.files:
                                f.folder_id = folder_id
                                f.folder_name = folders_name.get(folder_id)
                        files.extend(result.files)
                        for fd in result.folders:
                            if fd.folder_id in seen:
                                continue
                            seen.add(fd.folder_id)
                            parent = p_id if folder_id is not None else ""
                            folders_seen.append(
                                {"folder_id": fd.folder_id, "folder_name": fd.name, "parent_id": parent}
                            )
                            folders_name[fd.folder_id] = fd.name
                            folders_parent[fd.folder_id] = parent
                            queue.append((fd.folder_id, folder_id or ""))
        except Exception as e:
            logger.warning(
                f"[group_cloud_storage] traversal abort at folder={folder_id!r} for {group_id}: {e}"
            )
            return files, folders_seen, False, str(e)
        return files, folders_seen, True, None

    # ---------- Differential reconciliation (directory level) ----------

    async def _diff_traverse(
        self, group_id: str
    ) -> tuple[list, list, bool, str | None]:
        """Directory-level differential traversal: list the root once and
        each first-level folder once (single-level folder semantics); deeper
        levels are not recursed (less IO, lower risk-control exposure).

        Returns (files, folders_seen, complete, error) - complete=False means
        this reconciliation is absent (cloud rejected/timed out); the caller
        must not prune (frozen window).
        """
        files: list = []
        folders_seen: list = []
        folders_name: dict[str, str] = {}
        try:
            result = await self.api.list_group_root(group_id)
            files.extend(result.files)
            first_level: list[tuple[str, str]] = []  # (folder_id, name)
            for fd in result.folders:
                folders_seen.append(
                    {
                        "folder_id": fd.folder_id,
                        "folder_name": fd.name,
                        "parent_id": "",
                    }
                )
                folders_name[fd.folder_id] = fd.name
                first_level.append((fd.folder_id, ""))
            # Each first-level folder is listed once; if this directory is
            # absent, the whole reconciliation is absent and pruning for the
            # group is frozen. Bounded concurrency like the full traversal;
            # results are merged in input order and any failure aborts the
            # round (exception bubbles up -> complete=False, fail-closed).
            for start in range(0, len(first_level), self._traverse_concurrency):
                batch = first_level[start : start + self._traverse_concurrency]

                async def read_sub(fid: str):
                    return fid, await self.api.list_group_folder(group_id, fid)

                results = await asyncio.gather(
                    *(read_sub(fid) for fid, _parent in batch)
                )
                for fid, sub in results:
                    for f in sub.files:
                        f.folder_id = fid
                        f.folder_name = folders_name.get(fid)
                    files.extend(sub.files)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"[group_cloud_storage] diff traverse abort for {group_id}: {e}"
            )
            return files, folders_seen, False, str(e)
        return files, folders_seen, True, None

    async def run_diff_sync(
        self, group_id: str, lock: asyncio.Lock
    ) -> SyncResult:
        """Differential reconciliation: directory-level list upsert plus
        soft-delete of entries missing from the cloud listing (pruned
        immediately, balanced against additions).

        - complete=False (absent/failed) -> no pruning (frozen window;
          entries are pruned only after a successful reconciliation)
        - Shares upsert/orphan-cleanup gating with run_full_sync; full sync
          remains a manual-only path.
        """
        if lock.locked():
            return SyncResult(
                status=SyncStatus.FAILED, error="same group sync already running"
            )
        async with lock:
            files, folders, complete, error = await self._diff_traverse(group_id)
            if not complete:
                # Frozen: this reconciliation is absent; no local entries are
                # deleted (conservative pruning)
                logger.info(
                    f"[group_cloud_storage] diff frozen for {group_id}: {error}"
                )
                return SyncResult(
                    status=SyncStatus.FAILED, complete=False, error=error or "absent"
                )
            try:
                await self.store.upsert_folders(group_id, folders)
                resources = [Resource.from_group_file(group_id, f) for f in files]
                member_names = await self._load_member_names(group_id)
                self._apply_names(group_id, resources, member_names)
                indexed = await self.store.upsert_resources(resources)
                source_ids = {f.file_id for f in files}
                removed = await self.store.mark_missing_as_deleted(
                    group_id, True, source_ids
                )
                result = SyncResult(
                    status=SyncStatus.OK,
                    files_found=len(files),
                    files_indexed=indexed,
                    files_removed=removed,
                    complete=True,
                )
                logger.info(
                    f"[group_cloud_storage] diff sync {group_id}: "
                    f"found={len(files)} indexed={indexed} withered={removed}"
                )
                return result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[group_cloud_storage] diff sync failed for {group_id}")
                return SyncResult(status=SyncStatus.FAILED, complete=False, error=str(e))

    # ---------- Event indexing (group_upload) ----------

    async def index_event(self, raw: dict) -> bool:
        """Persist a OneBot group_upload notice event as a resource row.

        raw is event.message_obj.raw_message (the raw OneBot event dict).
        """
        if raw.get("notice_type") != "group_upload":
            return False
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "") or None
        f = raw.get("file") or {}
        file_id = str(f.get("id") or "")
        if not group_id or not file_id:
            return False
        res = Resource(
            group_id=group_id,
            type=ResourceType.FILE,
            name=str(f.get("name") or ""),
            source_ref=file_id,
            size=int(f.get("size", 0) or 0),
            uploader_id=user_id,
            busid=int(f.get("busid", 0) or 0),
            created_at=int(raw.get("time", 0) or 0),
        )
        await self.store.upsert_resources([res])
        # Event-driven table maintenance: volume part (*.partNN) events ->
        # auto-backfill volumes.source_ref
        await self._backfill_volume_on_event(group_id, res.name, file_id, res.busid)
        logger.info(
            f"[group_cloud_storage] event indexed {group_id}/{file_id} "
            f"({res.name}, {res.size}B)"
        )
        return True

    async def _backfill_volume_on_event(
        self, group_id: str, name: str, file_id: str, busid: int | None
    ) -> None:
        """Event-driven: match unbackfilled volume parts and backfill
        source_ref/busid.

        Covers event-driven recovery scenarios such as an interrupted upload
        or a member manually re-uploading a missing part (keeps the volumes
        table consistent).
        """
        import re

        if not re.search(r"\.part\d+$", name):
            return
        # volumes primary keys carry a group prefix (g:file:volgroup:*); the
        # SQL matches unbackfilled parts by part_name plus the group prefix
        await self.store.backfill_volume_by_part(group_id, name, file_id, busid or 0)
