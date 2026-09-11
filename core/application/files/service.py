"""Composition root — FileOpsService with all mixins composed."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.application.queue import OpQueue
from core.application.sync import ResourceSyncService
from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

from .crud import CrudMixin
from .download import DownloadMixin
from .folder import FolderMixin
from .volume import VolumeMixin


class FileOpsService(CrudMixin, VolumeMixin, DownloadMixin, FolderMixin):
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        sync: ResourceSyncService,
        tmp_dir: Path,
        page_size: int = 100,
        planner=None,
        config: dict | None = None,
        sync_locks: dict[str, asyncio.Lock] | None = None,
    ):
        from core.application.catalog import StoragePlanner

        self.api = api
        self.store = store
        self.queue = queue
        self.sync = sync
        self._planner = planner or StoragePlanner(store)
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.page_size = page_size
        # Config object injection: volume compression/verification are
        # mandatory built-in operations (no user-facing switches); the
        # config only carries general settings
        self._cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
        # 与 Services.lock_for 共享同一字典（bootstrap 注入同一实例）：
        # 队列 worker 跑文件操作的 sync 与 file_scan/sync 操作针对同一群,
        # 两套锁会让两个 run_full_sync 并发写同一群的索引。
        self._sync_locks = sync_locks if sync_locks is not None else {}
        # Listing cache: resolving many parts of one batch reuses a single
        # cloud listing (valid for 5s), turning N listing calls for an
        # N-part download into 1
        self._list_cache: dict[tuple, tuple[float, object]] = {}
        self._sweep_stale_tmp()

    def _sweep_stale_tmp(self, max_age_hours: float = 24.0) -> None:
        """Startup sweep: remove leftovers from crashed/cancelled long ops
        (reassembled recon_* files, replace_* temps, conv_* downloads,
        vol_*/unzip_* working dirs). Success-path files of a live process
        are younger than this and never match; the threshold only covers
        files orphaned across a restart."""
        import time as _time

        cutoff = _time.time() - max_age_hours * 3600
        removed = 0
        try:
            for entry in self.tmp_dir.iterdir():
                try:
                    if entry.is_dir():
                        import shutil

                        if entry.stat().st_mtime < cutoff:
                            shutil.rmtree(entry, ignore_errors=True)
                            removed += 1
                    elif entry.stat().st_mtime < cutoff:
                        entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            return
        if removed:
            logger.info(f"[file-ops] swept {removed} stale tmp entries at startup")

    # ---------- op dispatch (called by Main._op_handler) ----------

    async def handle(self, op) -> None:
        if op.kind == "upload":
            await self._do_upload(op)
        elif op.kind == "delete":
            await self._do_delete(op)
        elif op.kind == "move_file":
            await self._do_move(op)
        elif op.kind == "replace_name":
            await self._do_replace_name(op)
        elif op.kind == "create_folder":
            await self._do_create_folder(op)
        elif op.kind == "convert_volumes":
            await self._do_convert_volumes(op)
        else:
            raise ValueError(f"unknown file op kind: {op.kind}")
