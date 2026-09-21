from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger

from .video import album_has_media, remote_has_file

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}
# Downstream contracts enforce a 1..80 name/title (submit_essence_save raises,
# album uploads reject); clamp where the name is derived from the URL path.
_NAME_MAX = 80
# to_essence reads the whole downloaded document into memory as one string,
# while the download itself may be up to fetch_max_bytes (2GB by default).
# 32MiB ~= 8M chars ~= 2000 parts at the 4000-char chunk limit.
_ESSENCE_TEXT_MAX_BYTES = 32 * 1024 * 1024


def _clamp_name(name: str) -> str:
    """Clamp a name to the 1..80 downstream contract, keeping the extension so
    media-type detection still works. A long URL path segment used to fail the
    whole import *after* the download finished (and then retried 3 times)."""
    if 0 < len(name) <= _NAME_MAX:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot and 0 < len(suffix) <= 16:
        keep = max(1, _NAME_MAX - len(suffix) - 1)
        return f"{stem[:keep]}.{suffix}"
    return name[:_NAME_MAX] or "fetched"


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
        name = _clamp_name(
            op.payload.get("name") or Path(urlsplit(url).path).name or "fetched"
        )
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
            # detection goes by the name and the detected extension is
            # passed to compress() explicitly (BUG-12: compress re-checks
            # src.suffix, which would see .tmp and reject).
            media_ext = Path(name).suffix.lower()
            if (
                to_album
                and op.payload.get("lossy")
                and self.converter is not None
                and self.converter.is_media_ext(media_ext)
            ):
                downloaded = staged
                staged = await self.converter.compress(
                    staged,
                    op.payload.get("lossy_level") or "medium",
                    src_ext=media_ext,
                )
                if downloaded != staged:
                    downloaded.unlink(missing_ok=True)
                name = f"{Path(name).stem}{staged.suffix}"

            if to_essence:
                await self.queue.pause_check(op)
                # Replay guard: a replay derives a brand-new
                # essence_save task (new task_id), so without this the
                # group got the document twice, and the first batch's
                # message_ids lived only in the derived task's payload.
                if bool(getattr(op, "replayed", False)):
                    logger.info(
                        f"[ingest] fetch replay: essence task for {name} "
                        f"already submitted ({op.target}), skipping"
                    )
                    return
                # URL document read: text is sharded into essence messages.
                # Bounded (the download may be GBs) and off the event loop:
                # the previous plain read_text() loaded the whole body into
                # RAM and blocked the loop while doing it.
                if staged.stat().st_size > _ESSENCE_TEXT_MAX_BYTES:
                    # Static condition: the same downloaded file is always too
                    # big. A plain ValueError is classified as retriable by the
                    # queue, which re-downloaded the whole 32MiB+ document and
                    # failed identically 4 times (2/4/8s backoff). LOCAL_ERROR
                    # ends it on the first attempt -- the same fix as the
                    # sibling gates in video.py and album.py.
                    raise OneBotApiError(
                        OneBotErrorKind.LOCAL_ERROR,
                        "fetch",
                        "essence text source exceeds "
                        f"{_ESSENCE_TEXT_MAX_BYTES} bytes",
                    )
                text = await asyncio.to_thread(
                    staged.read_text, encoding="utf-8", errors="replace"
                )
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
                    # The album shows the uploaded file's own name; rename the
                    # neutral .tmp staging file to the declared name first
                    # (BUG-14: without this the album lists fetch_xxx.tmp).
                    upload_path = staged
                    declared = Path(name).name
                    if declared and declared != staged.name:
                        renamed = staged.with_name(declared)
                        # Same-declared-name reruns leave a stale tmp file
                        # behind (live 2026-09-12: the leftover made the
                        # rename a no-op and QQ showed fetch_xxx.tmp);
                        # staged files are transient, so replace it.
                        if renamed.exists():
                            renamed.unlink()
                        staged.replace(renamed)
                        upload_path = renamed
                    # Replay guard: same family as album.py's BUG-13
                    # skip -- a replay must not list the image twice.
                    if bool(getattr(op, "replayed", False)) and await album_has_media(
                        self.api, op.target, album_id, upload_path.name
                    ):
                        logger.info(
                            f"[ingest] fetch replay: {upload_path.name} already in "
                            f"'{album_name}' ({op.target}), skipping re-upload"
                        )
                    else:
                        await self.api.upload_image_to_qun_album(
                            op.target, album_id, album_name, upload_path.as_posix()
                        )
                    # Refresh must not fail the op after the irreversible
                    # upload (BUG-13: replay would re-upload the media)
                    await self._refresh_album_essence(op.target)
                    logger.info(f"[ingest] image -> album '{album_name}' in {op.target}")
                elif ext in _VIDEO_EXTS:
                    # Hand ownership to the long-video album task before this
                    # fetch task returns; that task deletes the staged file.
                    video_path = self.tmp_dir / (
                        f"fetch_video_{uuid.uuid4().hex[:10]}{ext}"
                    )
                    staged.replace(video_path)
                    # Replay guard: a replay must not derive a second
                    # video_album task (it would shard the same source
                    # twice). The source is re-downloaded on every replay
                    # (the finally below drops *staged*), so the only
                    # work skipped here is the derived submit.
                    if bool(getattr(op, "replayed", False)):
                        # The staged copy was moved out of *staged* (which
                        # the finally below only removes under its old name),
                        # and no derived task will ever consume it: drop it
                        # here or it leaks in tmp_dir.
                        video_path.unlink(missing_ok=True)
                        logger.info(
                            f"[ingest] fetch replay: video_album task for {name} "
                            f"already submitted ({op.target}), skipping"
                        )
                    else:
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
                # Replay guard: the group-file upload is not idempotent;
                # a replay that re-uploads leaves two copies under one
                # name (same family as the video direct-upload guard).
                already = bool(
                    getattr(op, "replayed", False)
                ) and await remote_has_file(self.api, op.target, name)
                if already:
                    logger.info(
                        f"[ingest] fetch replay: {name} already in "
                        f"{op.target}, skipping re-upload"
                    )
                else:
                    await self.api.upload_group_file(op.target, staged.as_posix(), name)
                lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
                result = await self.sync.run_full_sync(op.target, lock)
                if not result.ok:
                    logger.warning(f"[ingest] post-fetch sync failed: {result.error}")
                logger.info(f"[ingest] fetched -> file '{name}' in {op.target}")
        finally:
            staged.unlink(missing_ok=True)
