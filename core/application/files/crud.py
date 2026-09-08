"""File CRUD methods — rename, delete, move, upload (single-file)."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.enums import ResourceStatus, ResourceType
from core.domain.resource import Resource
from core.log import logger

from . import consts

if TYPE_CHECKING:
    from .service import FileOpsService


class CrudMixin:
    if TYPE_CHECKING:
        _service: FileOpsService

    # ---------- Upload (staged on Page -> group) ----------

    async def submit_upload(
        self, group_id: str, staged_path: str, name: str, folder_id: str | None = None
    ) -> str:
        return await self.queue.submit(
            "upload",
            target=group_id,
            payload={"path": staged_path, "name": name, "folder_id": folder_id or ""},
        )

    async def _do_upload(self, op) -> None:
        path = op.payload["path"]
        name = op.payload["name"]
        folder = op.payload.get("folder_id") or None
        logger.info(f"[file-ops] upload {name} -> group {op.target} ({path})")
        src = Path(path)
        if not src.exists() or not src.is_file():
            raise ValueError(f"staged file missing: {path}")
        size = src.stat().st_size
        # Capacity overflow switching: single-file direct upload only (volume
        # uploads keep their original target to avoid parent mapping drift).
        # The caller's group choice is honored unless that group itself cannot
        # fit the file (unknown capacity counts as fitting); only then does
        # the planner pick an overflow group.
        if not op.payload.get("parent_resource_id") and size > 0:
            groups = [
                g for g in await self.store.list_groups() if getattr(g, "managed", 1)
            ]
            requested = next(
                (g for g in groups if str(g.group_id) == str(op.target)), None
            )
            fits = (
                requested is None
                or requested.total_space <= 0
                or (requested.total_space - requested.used_space) >= size
            )
            pick = (
                None
                if fits
                else await self._planner.pick_group(groups, requested_bytes=size)
            )
            if pick is not None and str(pick.group_id) != str(op.target):
                logger.info(
                    f"[file-ops] upload target switch {op.target} -> {pick.group_id} "
                    f"(capacity overflow)"
                )
                op.target = pick.group_id
        threshold = consts.CHUNK_THRESHOLD_BYTES
        if size > threshold and not op.payload.get("parent_resource_id"):
            # Direct uploads above the threshold (paths that bypass the web
            # upload entry) still auto-volume (mandatory built-in op): stage
            # a parent resource then reuse the volume pipeline
            parent_id = f"volgroup:{uuid.uuid4().hex[:10]}"
            await self.store.upsert_resources(
                [
                    Resource(
                        group_id=op.target,
                        type=ResourceType.FILE,
                        name=name,
                        source_ref=parent_id,
                        size=size,
                        created_at=int(time.time()),
                        meta={
                            "volumes": True,
                            "compression": "zip-part",
                            "original_size": size,
                            "original_name": name,
                        },
                    )
                ]
            )
            op.payload["parent_resource_id"] = parent_id
            op.payload["parent_resource_id_full"] = f"{op.target}:file:{parent_id}"
        if size > threshold and op.payload.get("parent_resource_id"):
            # Volumes (WinRAR mode): upload a large file part by part with
            # checksum-verified reassembly (persisted in the cloud)
            await self._do_volume_upload(
                op, src, name, op.payload["parent_resource_id"], folder
            )
        else:
            await self.api.upload_group_file(op.target, path, name, folder_id=folder)
        # Trigger a full sync after upload to index the new file
        # (the single-file source_ref comes from the list API)
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if not result.ok:
            logger.warning(f"[file-ops] post-upload sync failed: {result.error}")
        if result.ok and op.payload.get("parent_resource_id_full"):
            await self.backfill_volume_refs(
                op.target, op.payload["parent_resource_id_full"]
            )
        # Clean up the staged file
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- Delete ----------

    async def submit_delete(self, group_id: str, id: int) -> str:
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        task_id = await self.queue.submit(
            "delete",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
            },
        )
        # Record the op in the operation stream (cloud deletes are irreversible
        # -> undo explicitly reports "irreversible")
        await self.queue.record_op(
            task_id,
            "delete",
            before={"group_id": group_id, "id": id, "name": detail["name"]},
            after={},
        )
        return task_id

    async def _do_delete(self, op) -> None:
        detail = await self.store.get_resource_detail(op.target, op.payload["id"])
        if detail and (detail.get("meta") or {}).get("volumes"):
            # Volume resource: delete each part from its own group (cross-group
            # storage) -> cascade-remove volume records -> soft-delete the parent
            vols = await self.store.list_volumes(detail["resource_id"])
            for v in vols:
                if not v.source_ref:
                    continue
                vg = v.group_id or op.target
                fresh = await self._resolve_file_ref(
                    vg,
                    v.part_name,
                    int(v.size or 0),
                    detail.get("folder_id") or None,
                )
                if fresh is None:
                    # Already gone from the cloud (NapCat ids are session-scoped
                    # handles: old ids cannot be reused) -> skip
                    logger.info(f"[file-ops] part {v.part_name} already gone")
                    continue
                await self.api.delete_group_file(vg, fresh[0], fresh[1])
            await self.store.remove_volumes(detail["resource_id"])
            await self.store.update_resource_fields(
                op.payload["id"], status=ResourceStatus.DELETED.value
            )
            logger.info(
                f"[file-ops] deleted volume-resource {detail['resource_id']} "
                f"({len(vols)} parts across groups)"
            )
            return
        fresh = await self._resolve_file_ref(
            op.target,
            (detail or {}).get("name") or op.payload.get("name") or "",
            0,
            (detail or {}).get("folder_id") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"])
        try:
            await self.api.delete_group_file(op.target, fid, busid)
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in ("invalid", "not found", "不存在")):
                logger.info(f"[file-ops] delete target already gone: {e}")
            else:
                raise
        await self.store.update_resource_fields(
            op.payload["id"], status=ResourceStatus.DELETED.value
        )
        logger.info(f"[file-ops] deleted {op.payload['file_id']} in {op.target}")

    # ---------- Rename / Move ----------

    async def submit_replace_name(self, group_id: str, id: int, new_name: str) -> str:
        """Rename (download-and-reupload semantics): download original -> reupload under the new name -> delete the old file."""
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        # Volume resources do not support this operation (too large)
        if (detail.get("meta") or {}).get("volumes"):
            raise ValueError("分卷资源不支持改名重传")
        task_id = await self.queue.submit(
            "replace_name",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "name": detail["name"],
                "new_name": new_name,
                "folder": detail.get("folder_id") or "",
            },
        )
        # Record the op in the operation stream (undo restores the previous name)
        await self.queue.record_op(
            task_id,
            "replace_name",
            before={"group_id": group_id, "id": id, "name": detail["name"]},
            after={"group_id": group_id, "id": id, "name": new_name},
        )
        return task_id

    async def _do_replace_name(self, op) -> None:
        """Download original bytes -> reupload under the new name (same group) -> delete the old file -> replace in the index."""
        import time as _t

        fresh = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"] or 0)
        data = await self._fetch_bytes(
            await self.api.get_group_file_url(
                op.target,
                fid,
                busid,
                op.payload["name"],
            )
        )
        if not data:
            raise ValueError("download returned empty content")
        staged = self.tmp_dir / f"replace_{op.payload['id']}_{int(_t.time())}.tmp"
        staged.write_bytes(data)
        await self.api.upload_group_file(
            op.target,
            staged.as_posix(),
            op.payload["new_name"],
            folder_id=op.payload.get("folder") or None,
        )
        # Delete the old file (re-resolve a fresh id: the old name still
        # exists after the reupload)
        fresh2 = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid2, busid2 = fresh2 or (op.payload["file_id"], op.payload["busid"] or 0)
        await self.api.delete_group_file(op.target, fid2, busid2)
        staged.unlink(missing_ok=True)
        # Index replacement: record the new file name (the new source_ref is
        # backfilled by a later file refresh; the old record is soft-deleted)
        await self.store.update_resource_fields(
            op.payload["id"], name=op.payload["new_name"]
        )
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        await self.sync.run_full_sync(op.target, lock)
        logger.info(
            f"[file-ops] replaced {op.payload['name']} -> "
            f"{op.payload['new_name']} in {op.target}"
        )

    async def submit_move(self, group_id: str, id: int, folder_id: str) -> str:
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        task_id = await self.queue.submit(
            "move_file",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "folder_id": folder_id,
                "folder": detail.get("folder_id") or "",
            },
        )
        # Record the op in the operation stream (undo moves it back)
        await self.queue.record_op(
            task_id,
            "move",
            before={
                "group_id": group_id,
                "id": id,
                "folder": detail.get("folder_id") or "",
            },
            after={"group_id": group_id, "id": id, "folder": folder_id},
        )
        return task_id

    async def _do_move(self, op) -> None:
        detail = await self.store.get_resource_detail(op.target, op.payload["id"])
        cpd = (
            (op.payload.get("folder") or "!/")
            if (op.payload.get("folder") or "").strip()
            else "!/"
        )
        fresh = await self._resolve_file_ref(
            op.target,
            (detail or {}).get("name") or "",
            0,
            (detail or {}).get("folder_id") or None,
        )
        fid = fresh[0] if fresh else op.payload["file_id"]
        await self.api.move_group_file(
            op.target,
            fid,
            cpd,
            f"!/{str(op.payload['folder_id']).lstrip('/')}"
            if op.payload.get("folder_id")
            else "!/",
        )
        await self.store.update_resource_fields(
            op.payload["id"], folder_id=op.payload["folder_id"]
        )
