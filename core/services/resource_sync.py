"""ResourceSyncService —— 采集编排 + 一致性（Slice 1/2/5，docs/02 §6）。

- 全量同步：根目录 + 递归子文件夹 → 批量幂等 UPSERT → 孤儿清理（门控 complete）→
  容量 → 统计 → 手工快照 → 同步日志
- 事件索引：group_upload → event_guaranteed 字段入库（docs/02 §7）
- 同群互斥：由调用方传入 asyncio.Lock（DoD #6）
- 失败：不崩溃；SyncResult 记录完整性与错误（DoD #8）
"""

from __future__ import annotations

import asyncio
import time

from core.log import logger

from core.domain.enums import ResourceType, SyncKind, SyncStatus
from core.domain.resource import GroupFileList, Resource
from core.domain.sync import Snapshot, SyncLog, SyncResult
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort


class SyncAborted(Exception):
    """全量同步因遍历失败中止（complete=False）。"""


class ResourceSyncService:
    def __init__(self, api: OneBotApiPort, store: MetaStorePort):
        self.api = api
        self.store = store
        self._member_names: dict[str, str] = {}

    # ---------- 上传者名称解析 ----------

    async def _load_member_names(self, group_id: str) -> dict[str, str]:
        """加载群成员昵称映射。返回独立 dict 避免并发覆盖。"""
        try:
            members = await self.api.list_group_members(group_id)
        except Exception as e:  # 名称解析失败不阻塞（DoD #8）
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

    # ---------- 全量同步 ----------

    async def run_full_sync(self, group_id: str, lock: asyncio.Lock) -> SyncResult:
        """执行一次受互斥保护的全量同步（返回 SyncResult，不抛业务异常）。"""
        if lock.locked():
            return SyncResult(
                status=SyncStatus.FAILED,
                error="same group sync already running (AC8)",
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

            # 目录持久化管理（v7）：全量刷新时重建该群目录实体
            await self.store.clear_folders(group_id)
            await self.store.upsert_folders(group_id, folders)

            resources = [Resource.from_group_file(group_id, f) for f in files]
            self._apply_names(group_id, resources, member_names)
            indexed = await self.store.upsert_resources(resources)

            # 孤儿清理（仅 complete=True，DoD #5 / AC9）
            source_ids = {f.file_id for f in files}
            await self.store.mark_missing_as_deleted(group_id, True, source_ids)

            # 容量 + 统计 + 手工快照（M5/M6）
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
        """BFS 遍历目录树，返回 (files, folders, complete, error)。"""
        files: list = []
        folders_seen: list = []
        folders_name: dict[str, str] = {}
        folders_parent: dict[str, str] = {}
        queue: list[tuple[str | None, str]] = [(None, "")]  # (folder_id, parent_id)
        try:
            while queue:
                folder_id, p_id = queue.pop(0)
                result: GroupFileList = (
                    await self.api.list_group_root(group_id)
                    if folder_id is None
                    else await self.api.list_group_folder(group_id, folder_id)
                )
                if folder_id is not None:
                    for f in result.files:
                        f.folder_id = folder_id
                        f.folder_name = folders_name.get(folder_id)
                files.extend(result.files)
                for fd in result.folders:
                    folders_seen.append(
                        {
                            "folder_id": fd.folder_id,
                            "folder_name": fd.name,
                            "parent_id": p_id if folder_id is not None else "",
                        }
                    )
                    folders_name[fd.folder_id] = fd.name
                    folders_parent[fd.folder_id] = p_id if folder_id is not None else ""
                    queue.append((fd.folder_id, folder_id or ""))
        except Exception as e:
            logger.warning(
                f"[group_cloud_storage] traversal abort at folder={folder_id!r} for {group_id}: {e}"
            )
            return files, folders_seen, False, str(e)
        return files, folders_seen, True, None

    # ---------- D-4 凋零差分对账（2026-09-01，W-8；目录级，替代定时全量） ----------

    async def _diff_traverse(
        self, group_id: str
    ) -> tuple[list, list, bool, str | None]:
        """目录级差分遍历：根目录 + 一级文件夹各拉一次列表（单级文件夹语义，
        N-03）；不递归更深层级（降低 IO 与风控）。

        返回 (files, folders_seen, complete, error)——complete=False 表示本次
        对账缺席（云端拒绝/超时），调用方不得执行凋零删除（冻结窗口）。
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
            # 一级文件夹各拉一次（本目录缺席 → 整次对账缺席，冻结该群凋零）
            for fid, _parent in first_level:
                sub = await self.api.list_group_folder(group_id, fid)
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
        """D-4 凋零差分对账：目录级列表 upsert + 云端消失条目软删（增补平衡）。

        - complete=False（缺席/失败）→ 不凋零（冻结窗口，对账成功才剔除）；
        - 与 run_full_sync 共享 upsert/孤儿清理门控，全量仍为手动例外。
        """
        if lock.locked():
            return SyncResult(
                status=SyncStatus.FAILED, error="same group sync already running"
            )
        async with lock:
            files, folders, complete, error = await self._diff_traverse(group_id)
            if not complete:
                # 冻结：本次对账缺席，不删除任何本地条目（保守凋零）
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

    # ---------- 事件索引（group_upload，M4 / AC3） ----------

    async def index_event(self, raw: dict) -> bool:
        """OneBot group_upload 通知事件入库（docs/02 §7 字段分级）。

        raw 为 event.message_obj.raw_message（原始 OneBot 事件 dict）。
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
        # 事件驱动库表自动维护：分卷片段（*.partNN）事件 → 自动回填 volumes.source_ref
        await self._backfill_volume_on_event(group_id, res.name, file_id, res.busid)
        logger.info(
            f"[group_cloud_storage] event indexed {group_id}/{file_id} "
            f"({res.name}, {res.size}B)"
        )
        return True

    async def _backfill_volume_on_event(
        self, group_id: str, name: str, file_id: str, busid: int | None
    ) -> None:
        """group_upload 事件驱动：匹配未回填的分卷 → 回填 source_ref/busid。

        覆盖「上传中断/群成员手动补传分卷」等事件驱动恢复场景（散居表一致性）。
        """
        import re

        if not re.search(r"\.part\d+$", name):
            return
        # volumes 主键含群前缀（g:file:volgroup:*）→ SQL 按 part_name + 群前缀匹配未回填卷
        await self.store.backfill_volume_by_part(group_id, name, file_id, busid or 0)
