"""Download link resolution and file download methods."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.log import logger

if TYPE_CHECKING:
    from .service import FileOpsService


class DownloadMixin:
    if TYPE_CHECKING:
        _service: FileOpsService

    async def _resolve_file_ref(
        self,
        group_id: str,
        name: str,
        size: int = 0,
        folder_id: str | None = None,
    ) -> tuple[str, int] | None:
        """Resolve a fresh file_id by name in real time.

        NapCat file_ids are session-scoped random handles (they expire across
        restarts/timeouts); stored ids serve only as a fallback. Resolve by
        name and size against a live listing before operating.
        """
        key = (str(group_id), str(folder_id or ""))
        now = time.monotonic()
        hit = self._list_cache.get(key)
        if hit and now - hit[0] < 5.0:
            lst = hit[1]
        else:
            try:
                lst = (
                    await self.api.list_group_folder(group_id, folder_id)
                    if folder_id
                    else await self.api.list_group_root(group_id)
                )
                self._list_cache[key] = (now, lst)
            except Exception as e:
                logger.debug(f"[file-ops] fresh resolve list failed for {name}: {e}")
                return None
        cands = [f for f in lst.files if f.name == name]
        if not cands:
            return None
        if size > 0:
            exact = [f for f in cands if f.size == size]
            if exact:
                cands = exact
        f = cands[0]
        return str(f.file_id), int(f.busid or 0)

    async def direct_link(self, group_id: str, id: int) -> tuple[str, str]:
        """Resolve one resource's live direct link (single file only; volumes
        have no single link). Like the web layer's stale-id lookup, falls back
        to a global id search when the (group, id) pair misses. Returns
        (url, name); raises ValueError when the resource is missing or is a
        volume resource; upstream errors propagate to the caller.
        """
        detail = await self.store.get_resource_detail(
            group_id, id
        ) or await self.store.get_resource_any(id)
        if not detail:
            raise ValueError(f"resource {id} not found")
        if (detail.get("meta") or {}).get("volumes"):
            raise ValueError(
                "分卷/视频资源不支持单链接：请使用「下载」/「转存到网盘」或本机下载服务地址"
            )
        name = detail.get("name") or "download"
        fresh = await self._resolve_file_ref(
            str(detail.get("group_id") or group_id),
            name,
            int(detail.get("size") or 0),
            detail.get("folder_id") or None,
        )
        fid, busid = fresh or (detail.get("source_ref"), detail.get("busid") or 0)
        url = await self.api.get_group_file_url(
            str(detail.get("group_id") or group_id), fid, busid, name
        )
        return url, name

    async def download_info(
        self, group_id: str, id: int, *, allow_incomplete: bool = False
    ) -> tuple[str, str]:
        """Return (download target, file name).

        - Single file: returns a live direct link (the caller proxies it as a
          stream)
        - Volumes: fetch every part -> verify each part sha256 -> merge in
          order -> return a local reassembled temporary file

        allow_incomplete: when some volume refs are missing (part deleted on
        the cloud / backfill pending), reassemble the available parts instead
        of failing; the total sha256 check is skipped for a partial result.
        """
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        name = detail["name"]
        if not self._is_volume_resource(detail):
            fresh = await self._resolve_file_ref(
                group_id,
                name,
                int(detail.get("size") or 0),
                detail.get("folder_id") or None,
            )
            fid, busid = fresh or (detail["source_ref"], detail["busid"] or 0)
            url = await self.api.get_group_file_url(group_id, fid, busid, name)
            return url, name
        vols = await self.store.list_volumes(detail["resource_id"])
        if not vols:
            raise ValueError("volume refs not ready (仍在上传/回填中)")
        ready = [v for v in vols if v.source_ref]
        missing = [v.seq for v in vols if not v.source_ref]
        if missing and not allow_incomplete:
            raise ValueError(
                f"volume refs not ready (缺失分卷 {missing}，仍在上传/回填中或已被删除)"
            )
        if not ready:
            raise ValueError("no downloadable volumes")
        kind = (detail.get("meta") or {}).get("kind") or "bytes"
        if kind == "video":
            if missing:
                raise ValueError(
                    f"视频分片缺失 {missing}，无法无损重组（视频不支持不完整下载）"
                )
            return await self._recon_video(
                group_id, name, ready, detail.get("folder_id") or None,
                (detail.get("meta") or {}).get("total_sha256"),
            )
        compression = (detail.get("meta") or {}).get("compression")
        out = self.tmp_dir / f"recon_{uuid.uuid4().hex[:10]}_{name}"
        extract_dir = self.tmp_dir / f"unzip_{uuid.uuid4().hex[:10]}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        ok = False
        try:
            with out.open("wb") as of:
                for v in sorted(ready, key=lambda x: x.seq):
                    # Cross-group volumes: fetch each part's URL from its own
                    # group (falls back to the parent group for legacy data)
                    vg = v.group_id or group_id
                    fresh = await self._resolve_file_ref(
                        vg,
                        v.part_name,
                        int(v.size or 0),
                        detail.get("folder_id") or None,
                    )
                    fid, busid = fresh or (v.source_ref, v.busid or 0)
                    url = await self.api.get_group_file_url(vg, fid, busid, v.part_name)
                    data = await self._fetch_bytes(url)
                    if v.sha256 and hashlib.sha256(data).hexdigest() != v.sha256:
                        raise ValueError(f"volume {v.seq} sha256 mismatch")
                    if compression == "zip-part":
                        # Each volume is an individual zip: extract the raw
                        # slice and append (split first, compress after)
                        import io
                        import zipfile

                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            inner = zf.namelist()[0]
                            of.write(zf.read(inner))
                    else:
                        of.write(data)
            meta_total = (detail.get("meta") or {}).get("total_sha256")
            if meta_total and not missing:
                if hashlib.sha256(out.read_bytes()).hexdigest() != meta_total:
                    raise ValueError("total sha256 mismatch")
            # Legacy whole-zip volumes -> extract to restore after verification
            # (reversible)
            if compression == "zip":
                import zipfile

                with zipfile.ZipFile(out) as zf:
                    zf.extractall(extract_dir)
                inner_name = Path((detail.get("meta") or {}).get("original_name") or name).name or name
                inner = extract_dir / inner_name
                if not inner.exists():
                    raise ValueError(f"zip reassemble missing {inner_name}")
                # Move out of extract_dir: the finally block wipes it, and the
                # caller still needs the restored file on disk
                restored = self.tmp_dir / f"recon_{uuid.uuid4().hex[:10]}_{inner_name}"
                inner.rename(restored)
                out.unlink(missing_ok=True)
                ok = True
                return restored.as_posix(), inner_name
            if compression == "zip-part" and not missing:
                # Rename to the logical original name once content verified
                original = (detail.get("meta") or {}).get("original_name")
                original = Path(original).name if original else name
                if original and original != name:
                    renamed = out.with_name(f"recon_{uuid.uuid4().hex[:10]}_{original}")
                    out.rename(renamed)
                    ok = True
                    return renamed.as_posix(), original
            ok = True
            return out.as_posix(), name
        finally:
            # A failed reassembly leaves no partial artifact behind
            import shutil

            shutil.rmtree(extract_dir, ignore_errors=True)
            if not ok:
                out.unlink(missing_ok=True)

    async def _recon_video(
        self, group_id: str, name: str, vols, folder: str | None = None,
        total_sha256: str | None = None,
    ) -> tuple[str, str]:
        """Reassembly of losslessly segmented video: fetch each segment
        (sha256 verified) -> merge via ffmpeg concat. Returns (path, name).
        """
        import subprocess
        import shutil as _sh

        if not _sh.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video reassemble")
        seg_dir = self.tmp_dir / f"vidrecon_{uuid.uuid4().hex[:10]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        out = self.tmp_dir / f"recon_{uuid.uuid4().hex[:10]}_{name}"
        try:
            list_file = seg_dir / "concat.txt"
            with list_file.open("w", encoding="utf-8") as lf:
                for v in vols:
                    vg = v.group_id or group_id
                    fresh = await self._resolve_file_ref(
                        vg, v.part_name, int(v.size or 0), folder
                    )
                    fid, busid = fresh or (v.source_ref, v.busid or 0)
                    url = await self.api.get_group_file_url(vg, fid, busid, v.part_name)
                    data = await self._fetch_bytes(url)
                    if v.sha256 and hashlib.sha256(data).hexdigest() != v.sha256:
                        raise ValueError(f"volume {v.seq} sha256 mismatch")
                    seg = seg_dir / v.part_name
                    seg.write_bytes(data)
                    lf.write(f"file '{seg.as_posix()}'\n")

            def _concat():
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        list_file.as_posix(),
                        "-c",
                        "copy",
                        out.as_posix(),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=14400,
                )
                if proc.returncode != 0:
                    raise ValueError(f"ffmpeg concat failed: {proc.stderr[-300:]}")

            import asyncio as _aio

            await _aio.to_thread(_concat)
            if total_sha256:
                actual = hashlib.sha256(out.read_bytes()).hexdigest()
                if actual != total_sha256:
                    raise ValueError("video total sha256 mismatch")
            return out.as_posix(), name
        finally:
            for f in seg_dir.glob("*"):
                if f.is_file() and f != out:
                    f.unlink(missing_ok=True)
            try:
                seg_dir.rmdir()
            except OSError:
                pass

    async def _fetch_bytes(self, url: str) -> bytes:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
