from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path

from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger

from .video import retriable_by_queue

# `upload_image_to_qun_album` is the *only* album-upload action any adapter
# exposes (ports/capabilities.GroupExtendsCapability, whose category doc reads
# "album image upload"); AlbumCapability declares no media-upload method at
# all. Adapters without the extension are marked UNSUPPORTED by
# NapCatBase._classify on the first failed call.
_ALBUM_UPLOAD_ACTION = "upload_image_to_qun_album"


class AlbumMixin:
    # Whether the protocol side can carry a *video* into a group album.
    #
    # False on every adapter today. The live NapCat protocol enforces it: a
    # video handed to `upload_image_to_qun_album` comes back
    # `retcode=100 "群相册上传仅支持 JPEG、PNG、GIF、WebP 或 BMP 图片"`, verified
    # against the real deployment on 2026-09-16. `_do_video_album` therefore
    # refuses the task up front instead of segmenting a file that can never be
    # uploaded. Flip this per adapter when one gains an album-video action.
    album_accepts_video = False

    async def _refresh_album_essence(self, group_id: str) -> None:
        """Best-effort album/essence resource refresh after an upload.

        The upload above this call is irreversible, so a refresh failure must
        not surface as an op error: the queue would replay the whole task and
        re-upload the same media (BUG-13). The next group scan reconciles the
        rows anyway (scan.py wraps the same calls in try/except).
        """
        try:
            albums = await self.api.get_qun_album_list(group_id)
            essences = await self.api.get_essence_msg_list(group_id)
            await self.store.upsert_album_essence(group_id, albums, essences)
        except Exception as e:
            logger.warning(
                f"[ingest] album/essence refresh skipped for {group_id} "
                f"(upload already done): {e}"
            )

    def _album_upload_ready(self) -> bool:
        """Whether the protocol side can push media into a group album.

        Reads the capability state NapCatBase caches for the extension action.
        UNKNOWN/SUPPORTED stay optimistic (the call itself is the real test);
        only a state already proven UNSUPPORTED short-circuits. Checking up
        front matters most for videos: without it a long file is segmented by
        ffmpeg first and only then has every segment rejected.
        """
        try:
            state = self.api.capability(_ALBUM_UPLOAD_ACTION)
        except Exception:
            return True  # no probe channel (stub adapters): let the call decide
        return state.value != "unsupported"

    def _require_album_upload(self) -> None:
        """Fail fast when the adapter has no album media upload extension.

        Raised as OneBotApiError/UNSUPPORTED, never ValueError: the op queue
        treats *every* non-OneBot exception as retriable and would back off
        2s/4s/8s before reporting the same static condition (14s plus four
        redundant album-list round trips). UNSUPPORTED is documented as
        "action not implemented by the adapter" and is non-retriable by design.
        """
        if self._album_upload_ready():
            return
        raise OneBotApiError(
            OneBotErrorKind.UNSUPPORTED,
            _ALBUM_UPLOAD_ACTION,
            "协议端不支持向群相册上传媒体。该能力为 NapCat group-extends 扩展："
            "请升级协议端，或改用群文件上传。",
        )

    def _require_album_video(self) -> None:
        """Fail fast when the adapter's album upload cannot take a video.

        Same reasoning as `_require_album_upload`: a static condition must be
        reported once, non-retriably, and before any ffmpeg work. Checking it
        here also keeps the message truthful — the failure a user sees is the
        real one instead of a downstream `retcode=100` per segment.
        """
        if self.album_accepts_video:
            return
        raise OneBotApiError(
            OneBotErrorKind.UNSUPPORTED,
            _ALBUM_UPLOAD_ACTION,
            "协议端群相册上传仅支持图片（NapCat retcode=100：仅支持 JPEG、PNG、GIF、"
            "WebP 或 BMP）。请改用「入群文件」，或在 QQ 客户端手动把视频加入相册。",
        )

    def _staged_source(self, op) -> Path:
        """The staged file this op should upload, tolerating a prior rename.

        A replay used to look only at ``payload["path"]``. The album paths
        rename that file to the declared name *in place* (so the album lists a
        human name rather than the uuid staging name), so on the second attempt
        the original path was gone: the image path raised a retriable
        ValueError, and the video path fell through to segmentation and
        reported a bogus "ffmpeg split failed: No such file or directory"
        (live 2026-09-16). Resolution order:
        the path recorded by a previous attempt, then the payload path, then a
        clear non-retriable error — a missing staged file is a local condition,
        not something a retry can fix.
        """
        recorded = op.payload.get("upload_path")
        if recorded and Path(recorded).exists():
            return Path(recorded)
        src = Path(op.payload["path"])
        if src.exists():
            return src
        raise OneBotApiError(
            OneBotErrorKind.LOCAL_ERROR,
            op.kind,
            f"暂存文件已不存在：{src.name}。上传可能已完成，或暂存已被清理；"
            "请重新发起上传。",
        )

    def _declared_upload_path(self, op, src: Path) -> Path:
        """Rename *src* to the declared name and remember it in the payload.

        The album shows the uploaded file's own name, while staged uploads
        arrive uuid-prefixed, so the file is renamed before uploading (BUG-14:
        without this the album lists the staging name). The new path is written
        back to the payload — the same retry-reuse convention as
        video.py's `parent_resource_id` — so a replay uploads the file that is
        actually on disk instead of a name that no longer resolves.
        """
        declared = Path(op.payload["name"] or "").name
        if not declared or declared == src.name:
            return src
        renamed = src.with_name(declared)
        # Same-declared-name reruns leave a stale tmp file behind (same live
        # failure as fetch BUG-14: the leftover made the rename a no-op and QQ
        # listed the staging name); staged files are transient, so replace it.
        if renamed.exists():
            renamed.unlink()
        src.replace(renamed)
        op.payload["upload_path"] = renamed.as_posix()
        return renamed

    @staticmethod
    def _is_replay(op) -> bool:
        """Whether this handler run is a re-entry: a queue retry *or* a
        pause -> resume (the queue arms `op.replayed` for both paths).

        op.retries only counts retries, and pause -> resume re-runs the handler
        from its first line with retries still 0, so deduping on it silently
        failed and the media was listed twice. The field is declared on the Op
        dataclass (Op.replayed); getattr keeps the check working for the duck
        -typed op objects contract tests pass in.
        """
        return bool(getattr(op, "replayed", False))

    async def _album_has_media(self, group_id: str, album_id: str, name: str) -> bool:
        """Whether the album already lists media called *name*.

        Consulted on replays only. An album upload is not idempotent: if the
        first attempt landed server-side but the call still raised (timeout),
        replaying it would list the same media twice — the same hazard
        `_refresh_album_essence` documents for its own BUG-13 note. Mirrors the
        "skip whatever is already there" convention of video.py's volume parts.
        """
        try:
            media = await self.api.get_group_album_media_list(group_id, album_id)
        except Exception:
            return False  # no probe channel (stub adapters): let the upload run
        wanted = Path(name).name
        for item in media or []:
            if not isinstance(item, dict):
                continue
            got = str(item.get("desc") or item.get("name") or item.get("file_name") or "")
            if got and Path(got).name == wanted:
                return True
        return False

    @staticmethod
    def _discard(*paths: Path) -> None:
        """Best-effort removal of staged files; never fails the op."""
        for path in paths:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    def _discard_staged(self, op) -> None:
        """Drop this op's staged file under either candidate name.

        The payload path is the original staging name; a rename records the
        declared-name path in `upload_path`. Both are transient, so removing
        whichever exists is always safe.
        """
        self._discard(
            *(
                Path(p)
                for p in (op.payload.get("upload_path"), op.payload.get("path"))
                if p
            )
        )

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
        except OneBotApiError as e:
            # "no such action" is static, not transient: re-raising it as
            # UNSUPPORTED keeps it non-retriable (a plain ValueError would be
            # replayed 3x, re-listing albums every time before failing anyway).
            if e.kind != OneBotErrorKind.UNSUPPORTED:
                raise
            raise OneBotApiError(
                OneBotErrorKind.UNSUPPORTED,
                "create_group_album",
                f"目标群没有相册「{wanted}」，且协议端无创建相册接口。"
                "请在 QQ 客户端中手动创建该相册后重试，或改用已有相册名称",
            ) from e
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
        """Queue a video for import into the group album.

        Segmentation lives in `_do_video_album`, which refuses the task when
        the adapter's album upload is image-only (every adapter today — see
        `album_accepts_video`). The task is still queued rather than rejected
        here so the refusal reaches the caller through the same task ledger as
        every other ingest outcome.
        """
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
        src = self._staged_source(op)
        album_name = op.payload["album_name"]
        try:
            self._require_album_upload()
            # _album_id's UNSUPPORTED (target album missing + no create API) is
            # the same terminal class as the gate above, so it goes through the
            # same exit: it used to sit outside the try, which leaked the
            # staged file on a refusal the op can never retry.
            album_id = await self._album_id(op.target, album_name)
        except OneBotApiError as e:
            if not retriable_by_queue(e):
                self._discard_staged(op)
            raise
        # The album shows the uploaded file's own name; staged uploads arrive
        # under a uuid-prefixed name, so rename to the declared name first
        # (BUG-14: without this the album lists the staging name).
        upload_path = self._declared_upload_path(op, src)
        # Replay guard: a replay (retry *or* pause -> resume) must not list the
        # same image twice. Only consulted when this *is* a replay, so the
        # happy path keeps its single round trip.
        if self._is_replay(op) and await self._album_has_media(
            op.target, album_id, upload_path.name
        ):
            logger.info(
                f"[ingest] album replay: {upload_path.name} already in "
                f"'{album_name}' ({op.target}), skipping re-upload"
            )
        else:
            await self.api.upload_image_to_qun_album(
                op.target, album_id, album_name, upload_path.as_posix()
            )
        # Album resource refresh (essence rows kept: both types re-collected
        # for this group)
        await self._refresh_album_essence(op.target)
        self.queue.publish(
            {
                "type": "done",
                "kind": "image_album",
                "target": op.target,
                "detail": op.payload["name"],
            }
        )
        # Clean the actually-uploaded file: after a rename it lives under the
        # declared name, and a leftover here is what trips the stale-name
        # guard above on the next same-name upload. Reached on success only —
        # a failed attempt must keep the file so the replay can reuse it.
        self._discard_staged(op)

    async def _do_video_album(self, op) -> None:
        """Album video import: segment by duration, then upload every segment.

        Dormant on every current adapter: it refuses the task unless
        ``album_accepts_video`` is set (see that attribute — the live protocol
        accepts images only). When enabled: videos shorter than
        video_segment_seconds upload directly (single segment, no split); at or
        above it they are losslessly segmented (ffmpeg -c copy) and each
        segment gets a semantic title `{stem} 第i/N段` so the album listing
        reads as one logical long video. Segments are delivered by
        `upload_image_to_qun_album` — the same action the image path uses, so
        album video support follows the adapter's album capability (probed
        below).
        """
        from core.application.composition.splitter import split_video

        # Both gates are static conditions: they must short-circuit before any
        # ffmpeg work (segmenting a long video only to have every segment
        # rejected wastes minutes) and must be non-retriable (the queue would
        # otherwise replay the whole split 3x). Being non-retriable, a refusal
        # is terminal — so the staged file is garbage and goes with it.
        try:
            self._require_album_upload()
            self._require_album_video()
            src = self._staged_source(op)
            stem = Path(op.payload["name"]).stem or "video"
            album_name = op.payload["album_name"]
            # Same terminal exit as the two gates above (see _do_image_album).
            album_id = await self._album_id(op.target, album_name)
        except OneBotApiError as e:
            if not retriable_by_queue(e):
                self._discard_staged(op)
            raise
        max_sec = int(getattr(self, "video_segment_seconds", 599))
        dur = await self._probe_duration(src.as_posix()) if hasattr(self, "_probe_duration") else None
        # Contract: <max_sec direct, >=max_sec split (e.g. 599s -> split)
        if dur is not None and dur < max_sec:
            # Short video: direct upload, no split. fetch-origin staging
            # arrives as fetch_video_<uuid>.ext -> rename to the declared name
            # first (same as _do_image_album / fetch BUG-14).
            upload_path = self._declared_upload_path(op, src)
            if self._is_replay(op) and await self._album_has_media(
                op.target, album_id, upload_path.name
            ):
                logger.info(
                    f"[ingest] album replay: {upload_path.name} already in "
                    f"'{album_name}' ({op.target}), skipping re-upload"
                )
            else:
                await self.api.upload_image_to_qun_album(
                    op.target, album_id, album_name, upload_path.as_posix()
                )
            await self._refresh_album_essence(op.target)
            self.queue.publish(
                {
                    "type": "done",
                    "kind": "video_album",
                    "target": op.target,
                    "detail": f"{op.payload['name']} (direct {int(dur)}s)",
                }
            )
            logger.info(
                f"[ingest] video -> album '{album_name}' direct ({int(dur)}s) in {op.target}"
            )
            self._discard_staged(op)
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
                # Same replay guard as the direct path: segment names are
                # deterministic, so a replay re-uploads nothing already there.
                if self._is_replay(op) and await self._album_has_media(
                    op.target, album_id, seg_title
                ):
                    logger.info(
                        f"[ingest] album replay: {seg_title} already in "
                        f"'{album_name}' ({op.target}), skipping re-upload"
                    )
                    continue
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
            await self._refresh_album_essence(op.target)
            logger.info(
                f"[ingest] media -> album '{album_name}' "
                f"({total} segments) in {op.target}"
            )
        finally:
            shutil.rmtree(seg_dir, ignore_errors=True)
        # Success only: a failed attempt keeps the source so the replay can
        # regenerate the segments (same convention as video.py's split branch).
        self._discard_staged(op)
