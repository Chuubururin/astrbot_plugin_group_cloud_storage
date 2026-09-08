from __future__ import annotations

import asyncio
import ast
import hashlib
import re
import shutil
import subprocess
import uuid
import time
from pathlib import Path

import httpx

from core.domain.enums import ResourceType
from core.domain.resource import Resource
from core.domain.sync import VolumeInfo
from core.log import logger
from core.application.composition.splitter import split_video

from .essence import CLOUD_CALL_TIMEOUT  # single cloud-call timeout definition (lives in essence)

VIDEO_SEGMENT_MAX_SECONDS = 600
VIDEO_PREVIEW_FRAMES = 9
VIDEO_PREVIEW_WIDTH = 320
VIDEO_PREVIEW_MAX_BYTES = 300 * 1024 * 1024


class VideoMixin:
    async def submit_video_upload(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        folder_id: str | None = None,
    ) -> str:
        """Video import: direct upload when within the limit; over-limit
        videos are segmented with ffmpeg (each segment <= the limit) and
        uploaded part by part."""
        return await self.queue.submit(
            "video_upload",
            target=group_id,
            payload={"path": staged_path, "name": name, "folder_id": folder_id or ""},
        )

    async def _download_video(self, url: str, cache_dir: Path, cache_key: str) -> Path:
        """Stream the video to local disk (size-capped; standalone method so
        tests can substitute it).

        The destination is built internally from ``cache_key`` (a hex digest
        under the plugin tmp cache dir) so the written path is never
        caller/URL-controlled.
        """
        safe_key = re.fullmatch(r"[0-9a-f]{8,64}", str(cache_key))
        if not safe_key:
            raise ValueError("invalid video preview cache key")
        # Filename is a strictly hex cache key under the plugin's own tmp
        # cache dir; enforce containment so the write target can never
        # escape it even if future callers pass unexpected arguments.
        base = Path(cache_dir).resolve()
        dest = (base / f"{safe_key.group(0)}.mp4").resolve()
        if dest.parent != base or dest.name != f"{safe_key.group(0)}.mp4":
            raise ValueError("invalid video preview destination")

        timeout = self.fetch_timeout
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = 0
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > VIDEO_PREVIEW_MAX_BYTES:
                            raise ValueError("视频超过预览大小上限（300MB）")
                        f.write(chunk)
        return dest

    async def _run_ffmpeg(self, args: list[str], timeout: int = 600) -> None:
        """Run an ffmpeg subprocess via to_thread (non-blocking); raises a
        clear error on failure."""

        def _run():
            proc = subprocess.run(
                ["ffmpeg"] + args, capture_output=True, text=True, timeout=timeout
            )
            if proc.returncode != 0:
                raise ValueError(
                    f"ffmpeg failed: {proc.stderr[-300:] or proc.stdout[-300:]}"
                )

        await asyncio.to_thread(_run)

    async def _extract_gif(self, src: Path, dest: Path, duration_s: float) -> None:
        """Extract N evenly spaced frames -> two-pass palette GIF (1 fps
        loop)."""
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video preview")
        frames_dir = dest.parent / dest.stem
        frames_dir.mkdir(parents=True, exist_ok=True)
        n = VIDEO_PREVIEW_FRAMES
        try:
            for i in range(n):
                t = (i + 0.5) * duration_s / n
                await self._run_ffmpeg(
                    [
                        "-y",
                        "-ss",
                        f"{t:.2f}",
                        "-i",
                        src.as_posix(),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale={VIDEO_PREVIEW_WIDTH}:-2:flags=lanczos",
                        (frames_dir / f"f{i:02d}.png").as_posix(),
                    ],
                    timeout=120,
                )
            await self._run_ffmpeg(
                [
                    "-y",
                    "-framerate",
                    "1",
                    "-i",
                    (frames_dir / "f%02d.png").as_posix(),
                    "-vf",
                    "split[a][b];[a]palettegen[p];[b][p]paletteuse",
                    "-loop",
                    "0",
                    dest.as_posix(),
                ],
                timeout=120,
            )
        finally:
            shutil.rmtree(frames_dir, ignore_errors=True)

    async def video_preview_gif(
        self, group_id: str, album_id: str, name: str = ""
    ) -> dict:
        """Album video keyframe GIF preview.

        Cloud video URL -> download (cached mp4) -> ffmpeg evenly spaced
        frame extraction -> palette GIF -> disk cache
        (tmp/video_preview/<sha1>.gif) -> base64 for inline frontend display.
        """
        import base64

        try:
            media = await asyncio.wait_for(
                self.api.get_group_album_media_list(group_id, album_id),
                timeout=CLOUD_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                "云端相册媒体拉取超时（QQ 会话退化或网络波动），请稍后重试"
            )

        entry = None
        if name:
            for m in media or []:
                v = m.get("video") or {}
                # Video items may be addressed by caption, video name, or id
                if name in (
                    str(m.get("desc") or ""),
                    str(v.get("name") or ""),
                    str(v.get("id") or ""),
                ):
                    entry = m
                    break
        if entry is None and not name:
            entry = next((m for m in (media or []) if m.get("video")), None)
        if entry is None:
            raise ValueError(f"视频条目不存在：{name or '(未命名)'}")

        v = entry.get("video") or {}
        # Multi-spec list: QQ NT feeds use camelCase videoUrl, NapCat-style
        # feeds use video_url, and some protocol ends return a stringified repr
        raw = v.get("videoUrl") or v.get("video_url")
        specs = raw if isinstance(raw, list) else []
        if isinstance(raw, str):
            try:
                specs = ast.literal_eval(raw)
            except Exception:
                specs = []
        if not isinstance(specs, list):
            specs = []
        urls = sorted(
            [
                s
                for s in specs
                if isinstance(s, dict) and (s.get("url") or {}).get("url")
            ],
            key=lambda s: int((s.get("url") or {}).get("width") or 0),
            reverse=True,
        )
        url = urls[0]["url"]["url"] if urls else v.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("该视频云端未提供可下载地址（无法生成预览）")
        duration_ms = int(v.get("videoTime") or v.get("video_time") or 0)

        vid = str(v.get("id") or entry.get("media_id") or name or url)
        cache_key = hashlib.sha256(f"{album_id}|{vid}".encode()).hexdigest()
        cache_dir = self.tmp_dir / "video_preview"
        cache_dir.mkdir(parents=True, exist_ok=True)
        gif_path = cache_dir / f"{cache_key}.gif"
        if not gif_path.exists():
            src = cache_dir / f"{cache_key}.mp4"
            if not src.exists():
                src = await self._download_video(url, cache_dir, cache_key)
            duration_s = await self._probe_duration(src.as_posix()) or (
                duration_ms / 1000.0 if duration_ms else 10.0
            )
            await self._extract_gif(src, gif_path, duration_s)

        data = gif_path.read_bytes()
        return {
            "gif_base64": base64.b64encode(data).decode("ascii"),
            "frames": VIDEO_PREVIEW_FRAMES,
            "duration_ms": duration_ms,
            "bytes": len(data),
            "note": "关键帧 GIF 预览（1 帧/秒循环）",
        }

    async def _probe_duration(self, path: str) -> float | None:
        """ffprobe duration (seconds); None when not probeable (treated as a
        direct upload)."""
        if not shutil.which("ffprobe"):
            return None

        def _run():
            try:
                out = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=nw=1:nk=1",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if out.returncode != 0:
                    return None
                return float(out.stdout.strip())
            except Exception:
                return None

        return await asyncio.to_thread(_run)

    async def _do_video_upload(self, op) -> None:
        path, name, folder = (
            op.payload["path"],
            op.payload["name"],
            op.payload.get("folder_id") or None,
        )
        src = Path(path)
        if not src.exists():
            raise ValueError(f"staged file missing: {path}")
        size = src.stat().st_size
        stem = Path(name).stem
        dur = await self._probe_duration(path)
        max_sec = self.video_segment_seconds
        # Contract: <max_sec direct, >=max_sec split (e.g. 599s -> split)
        if dur is None or dur < max_sec:
            await self.api.upload_group_file(op.target, path, name, folder_id=folder)
            lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
            result = await self.sync.run_full_sync(op.target, lock)
            if not result.ok:
                logger.warning(f"[ingest] post-video sync failed: {result.error}")
            logger.info(f"[ingest] video direct upload: {name} ({dur}s)")
            return
        # Long video: split storage (single logical resource + part volumes)
        parent_id = f"vidgroup:{uuid.uuid4().hex[:10]}"
        parent_key = f"{op.target}:file:{parent_id}"

        def _hash_source() -> str:
            h = hashlib.sha256()
            with open(src, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()

        total_sha_hex = await asyncio.to_thread(_hash_source)
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
                        "kind": "video",
                        "total_seconds": dur,
                        "total_sha256": total_sha_hex,
                    },
                )
            ]
        )
        seg_dir = self.tmp_dir / f"vid_{parent_id.split(':')[1]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # Lossless video segmentation goes through composition.splitter
        # (-c copy; same implementation as album splitting)
        segments = await split_video(src, seg_dir, stem, max_sec)
        total = len(segments)
        for seq, seg in enumerate(segments, 1):
            part_name = f"{stem}.part{seq:02d}.mp4"
            data = seg.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            await self.api.upload_group_file(
                op.target, seg.as_posix(), part_name, folder_id=folder
            )
            await self.store.insert_volumes(
                [
                    VolumeInfo(
                        parent_resource_id=parent_key,
                        seq=seq,
                        part_name=part_name,
                        size=len(data),
                        sha256=sha,
                        status="uploaded",
                    )
                ]
            )
            self.queue.publish(
                {
                    "type": "progress",
                    "kind": "video_upload",
                    "target": op.target,
                    "i": seq,
                    "n": total,
                    "part": part_name,
                }
            )
        # Backfill source_ref/busid (upload_group_file does not return a file_id)
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if result.ok:
            await self._backfill_volumes(op.target, parent_key)
        else:
            logger.warning(f"[ingest] video backfill sync failed: {result.error}")
        logger.info(
            f"[ingest] video split upload: {name} -> {total} parts (each <= {max_sec}s)"
        )
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass

    async def _backfill_volumes(self, group_id: str, parent_key: str) -> None:
        from core.domain.sync import ResourceQuery

        vols = await self.store.list_volumes(parent_key)
        if not vols:
            return
        page = await self.store.query_resources(
            ResourceQuery(group_id=group_id, page_size=200)
        )
        by_name = {it.name: it for it in page.items}
        for v in vols:
            hit = by_name.get(v.part_name)
            if hit:
                await self.store.update_volume_fields(
                    parent_key,
                    v.seq,
                    source_ref=hit.source_ref,
                    busid=hit.busid or 0,
                )
