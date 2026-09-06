from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from core.log import logger


class AlbumMixin:
    async def _album_id(self, group_id: str, album_name: str) -> str:
        async def _find(albums: list) -> str:
            for album in albums:
                if str(album.get("name") or album.get("album_name") or "").strip() == wanted:
                    album_id = str(album.get("album_id") or album.get("id") or "")
                    if album_id:
                        return album_id
            return ""

        wanted = (album_name or "AstrBot云盘").strip()
        albums = await self.api.get_qun_album_list(group_id)
        found = await _find(albums)
        if found:
            return found
        # Target album missing: try creating it via the protocol adapter
        # first (SnowLuma supports it; NapCat has no such extension API)
        try:
            await self.api.create_group_album(group_id, wanted)
        except Exception as e:
            raise ValueError(
                f"目标群没有相册「{wanted}」，且协议端创建相册失败（{e}）。"
                "请在 QQ 客户端中手动创建该相册后重试，或改用已有相册名称"
            ) from e
        # Creation succeeded but the album list may be eventually consistent;
        # re-query once
        found = await _find(await self.api.get_qun_album_list(group_id))
        if found:
            return found
        raise ValueError(
            f"相册「{wanted}」创建指令已发送但列表尚未刷新，请稍后重试"
        )

    async def submit_video_album(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        album_name: str = "AstrBot云盘",
    ) -> str:
        """Split a media file into segments and import them into the group
        album (each segment <= the duration limit)."""
        return await self.queue.submit(
            "video_album",
            target=group_id,
            payload={
                "path": staged_path,
                "name": name,
                "album_name": album_name or "AstrBot云盘",
            },
        )

    async def submit_image_album(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        album_name: str = "AstrBot云盘",
    ) -> str:
        """Import one image into the group album (upload_image_to_qun_album +
        album resource refresh)."""
        return await self.queue.submit(
            "image_album",
            target=group_id,
            payload={
                "path": staged_path,
                "name": name,
                "album_name": album_name or "AstrBot云盘",
            },
        )

    async def _do_image_album(self, op) -> None:
        src = Path(op.payload["path"])
        if not src.exists():
            raise ValueError(f"staged image missing: {src.name}")
        album_id = await self._album_id(op.target, op.payload["album_name"])
        await self.api.upload_image_to_qun_album(
            op.target, album_id, op.payload["album_name"], src.as_posix()
        )
        # Album resource refresh (essence rows kept: both types re-collected
        # for this group)
        albums = await self.api.get_qun_album_list(op.target)
        essences = await self.api.get_essence_msg_list(op.target)
        await self.store.upsert_album_essence(op.target, albums, essences)
        self.queue.publish(
            {
                "type": "done",
                "kind": "image_album",
                "target": op.target,
                "detail": op.payload["name"],
            }
        )
        src.unlink(missing_ok=True)

    async def _do_video_album(self, op) -> None:
        """Album video import (framework retained; the protocol side does not
        support album video upload yet).

        <599s videos upload directly (single segment, no split); >600s videos
        are losslessly segmented (ffmpeg -c copy) and each segment gets a
        semantic title `{stem} 第i/N段` so the album listing reads as one
        logical long video.
        """
        from core.application.composition.splitter import split_video

        src = Path(op.payload["path"])
        stem = Path(op.payload["name"]).stem or "video"
        album_name = op.payload["album_name"]
        album_id = await self._album_id(op.target, album_name)
        max_sec = int(getattr(self, "video_segment_seconds", 599))
        dur = await self._probe_duration(src.as_posix()) if hasattr(self, "_probe_duration") else None
        if dur is not None and dur <= max_sec:
            # Short video: direct upload, no split
            await self.api.upload_image_to_qun_album(
                op.target, album_id, album_name, src.as_posix()
            )
            albums = await self.api.get_qun_album_list(op.target)
            essences = await self.api.get_essence_msg_list(op.target)
            await self.store.upsert_album_essence(op.target, albums, essences)
            self.queue.publish(
                {
                    "type": "done",
                    "kind": "video_album",
                    "target": op.target,
                    "detail": f"{op.payload['name']} (direct {int(dur)}s)",
                }
            )
            src.unlink(missing_ok=True)
            logger.info(
                f"[ingest] video -> album '{album_name}' direct ({int(dur)}s) in {op.target}"
            )
            return
        seg_dir = self.tmp_dir / f"alb_{uuid.uuid4().hex[:10]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        try:
            segs = await split_video(src, seg_dir, stem, max_sec)
            total = len(segs)
            for i, seg in enumerate(segs, 1):
                # Semantic segment title: the album shows the filename, so
                # the shared stem + 段序/总数 makes the pieces read as one
                # logical long video
                seg_title = f"{stem} 第{i}-{total}段{seg.suffix}"
                seg_path = seg.with_name(seg_title)
                seg.rename(seg_path)
                await self.api.upload_image_to_qun_album(
                    op.target, album_id, album_name, seg_path.as_posix()
                )
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "video_album",
                        "target": op.target,
                        "i": i,
                        "n": total,
                        "part": f"{i}/{total}",
                    }
                )
            # Album resource refresh (essence rows kept)
            albums = await self.api.get_qun_album_list(op.target)
            essences = await self.api.get_essence_msg_list(op.target)
            await self.store.upsert_album_essence(op.target, albums, essences)
            logger.info(
                f"[ingest] media -> album '{album_name}' "
                f"({total} segments) in {op.target}"
            )
        finally:
            shutil.rmtree(seg_dir, ignore_errors=True)
        Path(op.payload["path"]).unlink(missing_ok=True)
