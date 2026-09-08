from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from core.log import logger

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}


class FetchMixin:
    async def submit_fetch(
        self,
        group_id: str,
        url: str,
        name: str = "",
        to_album: bool = False,
        album_name: str = "",
        to_essence: bool = False,
        convert_to: str = "",
        lossy: bool = False,
        lossy_level: str = "medium",
    ) -> str:
        """Queue an external URL ingest (http/https/sftp/smb).

        ``to_album`` accepts images and videos (videos use the long-video
        sharding pipeline); ``to_essence`` reads the downloaded document as
        text. The two album/essence targets are mutually exclusive.
        ``convert_to`` is an optional target extension such as ".mp4" or ".png".
        Album media is lossy re-encoded only when the uploader opted in
        (``lossy`` + tier high/medium/low; user-selected, irreversible).
        """
        if not url.lower().startswith(("http://", "https://", "sftp://", "smb://")):
            raise ValueError("unsupported scheme: only http/https/sftp/smb")
        if name and not (0 < len(name) <= 80):
            raise ValueError("name length 1..80")
        if to_album and to_essence:
            raise ValueError("to_album and to_essence are mutually exclusive")
        # Album target accepts images and videos; reject obvious text/archive
        # names early so a bad target fails before the network fetch.
        if to_album:
            guess = (name or Path(urlsplit(url).path).name or "").lower()
            ext = Path(guess).suffix
            if ext and ext not in (_IMAGE_EXTS | _VIDEO_EXTS):
                raise ValueError("album target only accepts images/videos")
        return await self.queue.submit(
            "fetch",
            target=group_id,
            payload={
                "url": url,
                "name": name,
                "to_album": to_album,
                "album_name": album_name,
                "to_essence": to_essence,
                "convert_to": convert_to,
                "lossy": bool(lossy),
                "lossy_level": lossy_level,
            },
        )

    async def _download(self, url: str, dest: Path) -> int:
        """Multi-protocol fetch (http/https/sftp/smb): delegated uniformly to
        TransferService."""
        if self.transfer is None:
            raise RuntimeError("transfer service not wired")
        return await self.transfer.download_to(url, dest)

    async def _do_fetch(self, op) -> None:
        url = op.payload["url"]
        name = op.payload.get("name") or Path(urlsplit(url).path).name or "fetched"
        to_album = bool(op.payload.get("to_album"))
        to_essence = bool(op.payload.get("to_essence"))
        convert_to = str(op.payload.get("convert_to") or "")
        staged = self.tmp_dir / f"fetch_{uuid.uuid4().hex[:10]}.tmp"
        try:
            await self.queue.pause_check(op)
            size = await self._download(url, staged)
            if size <= 0:
                raise ValueError("fetched empty content")
            logger.info(f"[ingest] fetched {url} ({size} bytes)")

            # Optional format conversion before any target branch.
            if convert_to and self.converter is not None and not to_essence:
                downloaded = staged
                staged = await self.converter.convert(staged, convert_to)
                if downloaded != staged:
                    downloaded.unlink(missing_ok=True)
                if Path(name).suffix.lower() != convert_to:
                    name = f"{Path(name).stem}{convert_to}"

            # User-selected lossy re-encode for album media (checkbox + tier
            # chosen at upload time, irreversible); non-media payloads skip
            # it. The staged file has a neutral .tmp suffix, so media
            # detection goes by the name.
            if (
                to_album
                and op.payload.get("lossy")
                and self.converter is not None
                and self.converter.is_media_ext(Path(name).suffix.lower())
            ):
                downloaded = staged
                staged = await self.converter.compress(
                    staged, op.payload.get("lossy_level") or "medium"
                )
                if downloaded != staged:
                    downloaded.unlink(missing_ok=True)
                name = f"{Path(name).stem}{staged.suffix}"

            if to_essence:
                await self.queue.pause_check(op)
                # URL document read: text is sharded into essence messages.
                text = staged.read_text(encoding="utf-8", errors="replace")
                task_id = await self.submit_essence_save(op.target, name, text)
                logger.info(f"[ingest] url doc -> essence '{name}' in {op.target} ({task_id})")
                return
            if to_album:
                album_name = op.payload.get("album_name") or "AstrBot云盘"
                # Trust the declared resource name first: downloads are staged
                # under a neutral .tmp suffix, so the real media type is lost
                # if we only inspect the staged filename.
                ext = Path(name).suffix.lower() or Path(staged.name).suffix.lower()
                if ext in _IMAGE_EXTS:
                    album_id = await self._album_id(op.target, album_name)
                    await self.queue.pause_check(op)
                    await self.api.upload_image_to_qun_album(
                        op.target, album_id, album_name, staged.as_posix()
                    )
                    albums = await self.api.get_qun_album_list(op.target)
                    essences = await self.api.get_essence_msg_list(op.target)
                    await self.store.upsert_album_essence(op.target, albums, essences)
                    logger.info(f"[ingest] image -> album '{album_name}' in {op.target}")
                elif ext in _VIDEO_EXTS:
                    # Hand ownership to the long-video album task before this
                    # fetch task returns; that task deletes the staged file.
                    video_path = self.tmp_dir / (
                        f"fetch_video_{uuid.uuid4().hex[:10]}{ext}"
                    )
                    staged.replace(video_path)
                    task_id = await self.submit_video_album(
                        op.target, video_path.as_posix(), name, album_name
                    )
                    logger.info(
                        f"[ingest] video -> album '{album_name}' in {op.target} ({task_id})"
                    )
                else:
                    raise ValueError(
                        "album target only accepts images/videos (jpg/png/gif/webp/bmp/mp4/mkv/...)"
                    )
            else:
                await self.queue.pause_check(op)
                await self.api.upload_group_file(op.target, staged.as_posix(), name)
                lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
                result = await self.sync.run_full_sync(op.target, lock)
                if not result.ok:
                    logger.warning(f"[ingest] post-fetch sync failed: {result.error}")
                logger.info(f"[ingest] fetched -> file '{name}' in {op.target}")
        finally:
            staged.unlink(missing_ok=True)
