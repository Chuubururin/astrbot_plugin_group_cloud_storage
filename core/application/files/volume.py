"""Volume conversion and management methods (WinRAR mode)."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.enums import ResourceType
from core.domain.resource import Resource
from core.domain.sync import ResourceQuery, VolumeInfo
from core.log import logger

from . import consts

if TYPE_CHECKING:
    from .service import FileOpsService


class VolumeMixin:
    if TYPE_CHECKING:
        _service: FileOpsService

    async def _do_volume_upload(
        self, op, src: Path, name: str, parent_id: str, folder: str | None
    ) -> None:
        """Split into volumes -> zip each volume -> upload part by part
        (split first, compress after; skipping already-uploaded parts =
        resumable upload).

        Volume naming carries the total count (`.partNNofMM.zip`) so the
        logical whole is recognizable from the cloud listing alone; the
        per-volume sha256 covers the uploaded (zipped) bytes and
        meta.total_sha256 covers the reassembled original content.
        """
        import zipfile

        # Pass 1: split the raw source into slices (CPU-bound: offload to thread)
        def _split_source() -> tuple[list[tuple[int, Path, int, str]], str]:
            total_sha = hashlib.sha256()
            cut_dir_local = self.tmp_dir / f"vol_{parent_id}"
            cut_dir_local.mkdir(parents=True, exist_ok=True)
            slices_local: list[tuple[int, Path, int, str]] = []
            with src.open("rb") as fh:
                seq = 1
                while True:
                    chunk = fh.read(consts.VOLUME_SIZE_BYTES)
                    if not chunk:
                        break
                    total_sha.update(chunk)
                    raw = cut_dir_local / f".raw_{seq:04d}"
                    raw.write_bytes(chunk)
                    slices_local.append((seq, raw, len(chunk), hashlib.sha256(chunk).hexdigest()))
                    seq += 1
            return slices_local, total_sha.hexdigest()

        slices, total_sha_hex = await asyncio.to_thread(_split_source)

        # The volumes primary key matches resources: the full resource_id
        # (download/backfill queries by detail)
        parent_key = op.payload.get("parent_resource_id_full") or parent_id
        cut_dir = self.tmp_dir / f"vol_{parent_id}"
        total_count = len(slices)
        stem = Path(name).stem or "file"
        volumes: list[VolumeInfo] = []

        for seq, raw, _raw_size, _raw_sha in slices:
            part_name = f"{stem}.part{seq:02d}of{total_count:02d}.zip"
            zpath = cut_dir / part_name

            # Compress + hash (CPU-bound: offload to thread). seq/stem/total_count
            # are bound via default args so the closure sees this iteration's values.
            def _compress_and_hash(_raw: Path = raw, _zpath: Path = zpath, _seq: int = seq) -> tuple[int, str]:
                with zipfile.ZipFile(_zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(_raw, arcname=f"{stem}.part{_seq:02d}of{total_count:02d}")
                _raw.unlink(missing_ok=True)
                zsize = _zpath.stat().st_size
                sha = hashlib.sha256(_zpath.read_bytes()).hexdigest()
                return zsize, sha

            zsize, vol_sha = await asyncio.to_thread(_compress_and_hash)
            volumes.append(
                VolumeInfo(
                    parent_resource_id=parent_key,
                    seq=seq,
                    part_name=part_name,
                    size=zsize,
                    sha256=vol_sha,
                    status="pending",
                )
            )
        await self.store.insert_volumes(volumes)  # idempotent; keeps existing part status
        existing_vols = await self.store.list_volumes(parent_key)
        for v in volumes:
            cur = next(
                (x for x in existing_vols if x.seq == v.seq),
                v,
            )
            if cur.status == "uploaded" and cur.source_ref:
                continue  # already uploaded -> skipped for resume
            await self.store.update_volume_fields(parent_key, v.seq, status="uploading")
            await self.api.upload_group_file(
                op.target,
                (cut_dir / v.part_name).as_posix(),
                v.part_name,
                folder_id=folder,
            )
            await self.store.update_volume_fields(
                parent_key,
                v.seq,
                status="uploaded",
                sha256=v.sha256,
                size=v.size,
            )
            self.queue.publish(
                {
                    "type": "progress",
                    "kind": "vol_upload",
                    "target": op.target,
                    "i": v.seq,
                    "n": total_count,
                    "part": v.part_name,
                }
            )
        # Parent resource meta records the total sha256 (used to verify
        # reassembly on download)
        import json

        detail = await self.store.get_resource_by_resource_id(
            op.payload.get("parent_resource_id_full") or parent_id
        )
        if detail:
            from core.application.composition.spec import encode_composition

            meta = dict(detail["meta"] or {})
            meta["volumes"] = True
            # zip-part: each volume is an individual zip (split first,
            # compress after); legacy "zip" = one zip split into raw slices
            meta["compression"] = "zip-part"
            meta["original_name"] = (
                op.payload.get("original_name") or op.payload.get("name") or name
            )
            if op.payload.get("original_size"):
                meta["original_size"] = int(op.payload["original_size"])
            meta["total_sha256"] = total_sha_hex
            meta["composition"] = encode_composition(
                "volumes", total_count, "binary", total_sha_hex
            )
            await self.store.update_resource_fields(
                detail["id"], meta=json.dumps(meta, ensure_ascii=False)
            )
        logger.info(f"[file-ops] volume upload done: {name} -> {len(volumes)} parts")
        # Clean up the local volume slices
        for v in volumes:
            (cut_dir / v.part_name).unlink(missing_ok=True)
        try:
            cut_dir.rmdir()
        except OSError:
            pass

    async def recommend_upload_group(
        self, kind: str = "file", requested_bytes: int = 0
    ) -> dict | None:
        """Recommend an upload group (data source for the default-rule endpoint).

        - kind=file (group file): the group with the smallest group id whose
          free space is greater than the size to upload;
        - kind=album/essence: the group with the smallest group id (quota
          unknown, no capacity precheck);
        - Candidates are managed, online (hidden=0) groups only; returns None
          when no candidate exists.
        """
        groups = await self.store.list_groups()
        candidates = [g for g in groups if getattr(g, "managed", 1)]
        if not candidates:
            return None
        if kind in ("album", "essence"):
            pick = await self._planner.pick_min_group_id(candidates)
        else:
            pick = await self._planner.pick_min_group_for_size(
                candidates, requested_bytes=int(requested_bytes or 0)
            )
        if pick is None:
            return None
        return {
            "group_id": pick.group_id,
            "group_name": pick.shown_name or pick.group_name or pick.group_id,
            "role": pick.role,
            "sort_order": pick.sort_order,
        }

    async def submit_volume_upload(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        folder_id: str | None = None,
    ) -> str:
        """Volume upload entry point: pre-create the parent resource
        (meta.volumes marker) -> enqueue.

        Split first, compress after (built-in operation): the source file is
        split into raw volumes in the queue task, each volume zipped
        individually; meta records compression=zip-part and
        original_size/original_name — on download, each volume is unzipped,
        the raw content reassembled and verified via meta.total_sha256.
        """
        import uuid as _uuid

        parent_id = f"volgroup:{_uuid.uuid4().hex[:10]}"
        meta: dict = {"volumes": True}

        src = Path(staged_path)
        meta.update(
            compression="zip-part",
            original_size=src.stat().st_size,
            original_name=name,
        )
        total = src.stat().st_size
        await self.store.upsert_resources(
            [
                Resource(
                    group_id=group_id,
                    type=ResourceType.FILE,
                    name=name,
                    source_ref=parent_id,
                    size=total,
                    created_at=int(time.time()),
                    meta=meta,
                )
            ]
        )
        return await self.queue.submit(
            "upload",
            target=group_id,
            payload={
                "path": staged_path,
                "name": name,
                "folder_id": folder_id or "",
                "parent_resource_id": parent_id,
                "parent_resource_id_full": f"{group_id}:file:{parent_id}",
            },
        )

    async def backfill_volume_refs(
        self, group_id: str, parent_resource_id_full: str
    ) -> None:
        """Backfill volume source_ref/busid after sync
        (upload_group_file does not return a file_id).
        """
        from core.domain.sync import ResourceQuery as _RQ

        vols = await self.store.list_volumes(parent_resource_id_full)
        if not vols:
            return
        page = await self.store.query_resources(
            _RQ(group_id=group_id, page_size=1000), fold_parts=False
        )
        by_name = {it.name: it for it in page.items}
        for v in vols:
            hit = by_name.get(v.part_name)
            if hit:
                await self.store.update_volume_fields(
                    parent_resource_id_full,
                    v.seq,
                    source_ref=hit.source_ref,
                    busid=hit.busid or 0,
                )
        logger.info(f"[file-ops] volume backfill: {len(vols)} parts -> {group_id}")

    def _is_volume_resource(self, detail: dict) -> bool:
        return bool((detail.get("meta") or {}).get("volumes"))

    @staticmethod
    def _is_current_account_upload(detail: dict, account_id: str | None) -> bool:
        """Return whether the stored file uploader matches the group account.

        Existing cloud files are re-uploaded and then their originals are
        deleted during conversion. Missing identity is therefore unsafe and
        must be treated the same as another account's upload.
        """
        uploader_id = str(detail.get("uploader_id") or "")
        current_id = str(account_id or "")
        return bool(uploader_id and current_id and uploader_id == current_id)

    async def _group_account_id(self, group_id: str) -> str:
        """Return the account bound to a group, or an empty identity."""
        groups = await self.store.list_groups()
        group = next((g for g in groups if str(g.group_id) == str(group_id)), None)
        return str(getattr(group, "account_id", "") or "") if group else ""

    async def submit_convert_volumes(self, group_id: str, id: int) -> str:
        """Split an existing cloud file into volumes: download -> split into
        volumes (each zipped individually, reversible) -> upload part by part
        -> delete the original.
        """
        from core.application.composition.spec import is_composite

        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        if is_composite(detail.get("meta")):
            raise ValueError("该资源已是组合形态（分卷/分片），无需转换")
        account_id = await self._group_account_id(group_id)
        if not self._is_current_account_upload(detail, account_id):
            raise ValueError("仅支持当前群归属账号上传的文件进行分卷")
        # The threshold is read via the module attribute at call time so tests
        # can patch files.consts uniformly.
        threshold = consts.CHUNK_THRESHOLD_BYTES
        if int(detail.get("size") or 0) <= threshold:
            raise ValueError(f"文件小于分卷阈值（{consts.threshold_label()}）")
        return await self.queue.submit(
            "convert_volumes",
            target=group_id,
            payload={
                "id": id,
                "name": detail["name"],
                "folder": detail.get("folder_id") or "",
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "resource_id": detail["resource_id"],
                "original_name": detail["name"],
            },
        )

    async def sweep_convert_volumes(self, group_id: str, limit: int = 5) -> int:
        """Built-in sweep after a sync: cloud files over the volume threshold
        are converted to volumes automatically (built-in operation; no user
        action). Only files uploaded by the group-bound account qualify —
        the original must be deletable by that account. Composite resources
        and files with a queued conversion are skipped; the per-sweep limit
        keeps the first run after deployment from flooding the queue
        (remaining files are picked up by later sweeps). Returns the number
        of conversions submitted.
        """
        from core.application.composition.spec import is_composite

        submitted = 0
        account_id = await self._group_account_id(group_id)
        page_num = 1
        while submitted < limit:
            page = await self.store.query_resources(
                ResourceQuery(group_id=group_id, page_size=200, page=page_num),
                fold_parts=False,
            )
            items = page.items or []
            if not items:
                break
            page_num += 1
            for it in items:
                if submitted >= limit:
                    break
                detail = await self.store.get_resource_detail(group_id, it.id)
                if not detail or not self._is_current_account_upload(detail, account_id):
                    logger.debug(
                        f"[file-ops] volume sweep skip (not owner upload): "
                        f"{group_id}/{it.name}"
                    )
                    continue
                if int(it.size or 0) <= consts.CHUNK_THRESHOLD_BYTES:
                    continue
                if is_composite(detail.get("meta")):
                    continue
                if self.queue.has_pending("convert_volumes", "id", it.id):
                    continue
                try:
                    await self.submit_convert_volumes(group_id, it.id)
                    submitted += 1
                except ValueError:
                    continue
            if len(items) < 200:
                break
        return submitted

    async def _do_convert_volumes(self, op) -> None:
        """Download the original -> reuse the volume upload pipeline -> delete
        the cloud original -> sync the index.
        """
        fresh = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"] or 0)
        data = await self._fetch_bytes(
            await self.api.get_group_file_url(op.target, fid, busid, op.payload["name"])
        )
        if not data:
            raise ValueError("download returned empty content")
        src = self.tmp_dir / f"conv_{op.payload['id']}_{uuid.uuid4().hex[:8]}.tmp"
        src.write_bytes(data)
        # Split first, compress after: the raw download goes straight into
        # the volume pipeline (each volume is zipped individually there;
        # download reassembly extracts it automatically)
        op.payload["original_size"] = src.stat().st_size
        try:
            # Reuse the volume upload pipeline: use the existing resource_id as
            # the parent key (in-place index conversion)
            op.payload["parent_resource_id"] = op.payload["id"]
            op.payload["parent_resource_id_full"] = op.payload["resource_id"]
            await self._do_volume_upload(
                op,
                src,
                op.payload["name"],
                op.payload["id"],
                op.payload.get("folder") or None,
            )
        finally:
            src.unlink(missing_ok=True)
        # Delete the cloud original (the old file remains after re-upload)
        fresh2 = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid2, busid2 = fresh2 or (op.payload["file_id"], op.payload["busid"] or 0)
        await self.api.delete_group_file(op.target, fid2, busid2)
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if result.ok and op.payload.get("parent_resource_id_full"):
            await self.backfill_volume_refs(
                op.target, op.payload["parent_resource_id_full"]
            )
        logger.info(f"[file-ops] converted {op.payload['name']} to volumes")
