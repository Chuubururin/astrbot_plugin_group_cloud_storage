"""CloudIngestService —— 云端入库编排（云存储资源池的写入通路）。

三条入库通路（全部经 OpQueue 限速/退避/SSE，docs/12）：
1. 精华文本入库：QQ 精华单条字符上限（默认 4500）→ 长文本自动拆分
   → 逐段 send_group_msg + set_essence_msg → 单逻辑资源（meta.parts 分片索引）；
   读回 = 按分片标记从云端精华列表重建全文（云端为源）。
2. HTTP/HTTPS/FTP 外部文件入库：非本机文件多途径传输 → 流式下载到暂存
   → 群文件；图片可选上传群相册（upload_image_to_qun_album）。
3. 长视频拆分存储：群相册视频时长上限（默认 600s）→ ffmpeg 强制关键帧分段
   （每段 ≤600s）→ 逐段上传群文件 → 单逻辑资源（volumes 表分片，下载 ffmpeg
   concat 重组）。
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote

import httpx

from core.domain.enums import ResourceType
from core.domain.resource import Resource
from core.domain.sync import VolumeInfo
from core.log import logger
from core.services.op_queue import OpQueue
from core.services.resource_sync import ResourceSyncService
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

# QQ 精华单条字符上限（OneBot 生态实测口径；可配置覆盖）
ESSENCE_CHUNK_MAX_CHARS = 4500
# 群相册视频时长上限（秒；可配置覆盖）
VIDEO_SEGMENT_MAX_SECONDS = 600
# 外部拉取上限/超时（防御性护栏）
FETCH_MAX_BYTES = 2 * 1024**3
FETCH_TIMEOUT_SEC = 180.0

# 分片标记（重建全文时按此前缀定位分片；刻意不使用 emoji）
_PART_MARK = "[云盘|{title}|{seq}/{total}]"

# Image extension whitelist (album upload entry).
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# Video extensions accepted by the long-video album pipeline.
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}


# 云端实时拉取超时（秒）：QQ 会话退化/网络波动时预览不挂起（可被测试 monkeypatch）
CLOUD_CALL_TIMEOUT = 12.0

# 相册视频关键帧 GIF 预览（v2.5）
VIDEO_PREVIEW_FRAMES = 9  # 均匀抽取帧数
VIDEO_PREVIEW_WIDTH = 320  # 预览宽度（等比缩放）
VIDEO_PREVIEW_MAX_BYTES = 300 * 1024 * 1024  # 下载上限，防滥用


# 文本拆分收敛到组合模块（v2.8）：core/composition/splitter（保持同名再导出兼容测试）
from core.composition.splitter import (  # noqa: E402
    effective_chunk_limit,
    split_text,
)
from core.composition.spec import encode_composition  # noqa: E402


class CloudIngestService:
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        sync: ResourceSyncService,
        tmp_dir: Path,
        config: dict | None = None,
        transfer=None,
        converter=None,
    ):
        self.api = api
        self.store = store
        self.queue = queue
        self.sync = sync
        self.transfer = transfer  # v1.3: multi-protocol pull (smb etc.)
        self.converter = converter  # optional W2-B converter for URL uploads
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        cfg = config or {}
        self.essence_chunk_chars = int(
            cfg.get("essence_chunk_size", ESSENCE_CHUNK_MAX_CHARS)
            or ESSENCE_CHUNK_MAX_CHARS
        )
        self.video_segment_seconds = int(
            cfg.get("video_segment_seconds", VIDEO_SEGMENT_MAX_SECONDS)
            or VIDEO_SEGMENT_MAX_SECONDS
        )
        self.fetch_max_bytes = int(
            cfg.get("fetch_max_bytes", FETCH_MAX_BYTES) or FETCH_MAX_BYTES
        )
        self.fetch_timeout = float(
            cfg.get("fetch_timeout_sec", FETCH_TIMEOUT_SEC) or FETCH_TIMEOUT_SEC
        )
        self._sync_locks: dict[str, asyncio.Lock] = {}

    # ==================== 1. 精华文本入库（拆分存储） ====================

    async def submit_essence_save(self, group_id: str, title: str, text: str) -> str:
        """登记精华文本入库（长文本按字符上限拆分，逐段发送并设精）。"""
        if not (0 < len(title) <= 80):
            raise ValueError("title length 1..80")
        if not text.strip():
            raise ValueError("text empty")
        return await self.queue.submit(
            "essence_save",
            target=group_id,
            payload={"title": title, "text": text},
        )

    @staticmethod
    def _marker(title: str, seq: int, total: int) -> str:
        return _PART_MARK.format(title=title, seq=seq, total=total)

    async def _part_essence_set(
        self, group_id: str, title: str, seq: int, total: int, chunk: str
    ) -> str:
        """发送一段并设为精华，带云端回读验证（QQ 偶发丢设 → 重发重设，≤3 次）。

        返回最终 message_id；重发时旧消息自动成为普通消息（无副作用）。
        """
        marker = self._marker(title, seq, total)
        msg = f"{marker}\n{chunk}"
        for attempt in range(3):
            r = await self.api.send_group_msg(
                group_id, [{"type": "text", "data": {"text": msg}}]
            )
            mid = str((r or {}).get("message_id") or "")
            if not mid:
                raise ValueError("send_group_msg returned no message_id")
            await self.api.set_essence_msg(mid)
            try:
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=CLOUD_CALL_TIMEOUT,
                )
            except Exception:
                essences = []
            if any(self._extract_text(e).startswith(marker) for e in essences):
                return mid
            logger.warning(
                f"[ingest] essence part {seq}/{total} not confirmed, retry {attempt + 1}"
            )
            await asyncio.sleep(1.5)
        raise ValueError(f"essence part {seq}/{total} not confirmed after 3 attempts")

    async def _do_essence_save(self, op) -> None:
        title, text = op.payload["title"], op.payload["text"]
        total = len(split_text(text, self.essence_chunk_chars))
        # 正文上限扣除标记开销（QQ 单条精华 ≤4500 字，含标记；防云端截断）
        limit = effective_chunk_limit(title, total, self.essence_chunk_chars)
        chunks = split_text(text, limit)
        total = len(chunks)
        parts: list[dict] = []
        for seq, chunk in enumerate(chunks, 1):
            mid = await self._part_essence_set(op.target, title, seq, total, chunk)
            # 分片文本本地冗余：云端不可用时仍可离线重建全文（v2.4）
            parts.append(
                {"seq": seq, "message_id": mid, "chars": len(chunk), "text": chunk}
            )
            self.queue.publish(
                {
                    "type": "progress",
                    "kind": "essence_save",
                    "target": op.target,
                    "i": seq,
                    "n": total,
                    "part": f"{seq}/{total}",
                }
            )
        await self.store.upsert_resources(
            [
                Resource(
                    group_id=op.target,
                    type=ResourceType.ESSENCE,
                    name=title,
                    source_ref=f"text:{uuid.uuid4().hex[:10]}",
                    size=len(text),
                    created_at=int(time.time()),
                    meta={
                        "kind": "text_split",
                        "parts": parts,
                        "summary": text[:200].replace("\n", " "),
                        "composition": encode_composition(
                            "text_split", total, "marker"
                        ),
                    },
                )
            ]
        )
        logger.info(f"[ingest] essence saved: {title} -> {total} parts in {op.target}")

    @staticmethod
    def _extract_text(e: dict) -> str:
        content = e.get("content")
        if isinstance(content, list):
            return " ".join(
                str((s.get("data") or {}).get("text") or "")
                for s in content
                if isinstance(s, dict) and s.get("type") == "text"
            ).strip()
        return str(content or e.get("text") or "").strip()

    async def essence_full_text(self, group_id: str, id: int) -> tuple[str, list[int]]:
        """从云端精华列表重建分片全文；返回 (全文, 缺失分片序号)。

        精华列表为最终一致（设精后可能有短暂延迟），缺失时按 1s 间隔重试 3 次。
        """
        row = await self.store.get_resource_any(id)
        if not row or str(row.get("group_id")) != str(group_id):
            raise ValueError(f"resource {id} not found in group {group_id}")
        meta = row.get("meta") or {}
        parts = list(meta.get("parts") or [])
        if not parts:
            # 非拆分精华：优先从云端取该条精华完整内容（source_ref=精华条目 message_id）
            try:
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=CLOUD_CALL_TIMEOUT,
                )
            except Exception:
                essences = []
            for e in essences:
                if str(e.get("message_id") or "") == str(row.get("source_ref") or ""):
                    text = self._extract_text(e)
                    if text:
                        return text, []
            # 云端未命中 → 本地摘要兜底
            return (meta.get("summary") or row.get("name") or ""), []
        total = len(parts)
        # 本地缓存快路径（v2.4）：保存时已冗余分片文本 → 无需云端即时重建
        if parts and all(p.get("text") for p in parts):
            ordered = [p["text"] for p in sorted(parts, key=lambda x: x["seq"])]
            return "\n".join(ordered), []
        by_prefix: dict[int, str] = {}
        cloud_timeout = False
        cloud_ok = False
        for attempt in range(4):
            try:
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=CLOUD_CALL_TIMEOUT,
                )
                cloud_ok = True
            except asyncio.TimeoutError:
                cloud_timeout = True
                essences = []
            except Exception:
                essences = []
            for e in essences:
                text = self._extract_text(e)
                for p in parts:
                    prefix = self._marker(row["name"], p["seq"], total)
                    if text.startswith(prefix):
                        by_prefix[p["seq"]] = text[len(prefix) :].lstrip("\n")
            missing = sorted(p["seq"] for p in parts if p["seq"] not in by_prefix)
            if not missing or attempt == 3:
                break
            logger.debug(
                f"[ingest] essence parts missing {missing}, retry {attempt + 1}"
            )
            await asyncio.sleep(1.0)
        if missing:
            import json as _json

            raw_sample = [
                {
                    k: (str(v)[:80])
                    for k, v in (e or {}).items()
                    if k in ("message_id", "msg_seq", "content", "text")
                }
                for e in essences[:3]
            ]
            logger.warning(
                f"[ingest] essence rebuild incomplete: {missing} of {total} "
                f"(raw: {_json.dumps(raw_sample, ensure_ascii=False)})"
            )
        if not by_prefix and not cloud_ok:
            # 云端完全不可用：抛清晰错误（网络/会话退化），而非空模态
            if cloud_timeout:
                raise TimeoutError(
                    "云端精华列表拉取超时（QQ 会话退化或网络波动），请稍后重试"
                )
            raise ValueError("云端精华列表不可用，且本地无缓存分片")
        ordered = [
            by_prefix[p["seq"]]
            for p in sorted(parts, key=lambda x: x["seq"])
            if p["seq"] in by_prefix
        ]
        return "\n".join(ordered), missing

    # ==================== 2. HTTP/HTTPS/FTP 外部文件入库 ====================

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
    ) -> str:
        """Queue an external URL ingest (http/https/ftp/smb).

        ``to_album`` accepts images and videos (videos use the long-video
        sharding pipeline); ``to_essence`` reads the downloaded document as
        text. The two album/essence targets are mutually exclusive.
        ``convert_to`` is an optional target extension such as ".mp4" or ".png".
        ``lossy`` re-encodes album media before upload (irreversible).
        """
        if not url.lower().startswith(("http://", "https://", "ftp://", "smb://")):
            raise ValueError("unsupported scheme: only http/https/ftp/smb")
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
                "lossy": lossy,
            },
        )

    async def _download(self, url: str, dest: Path) -> int:
        """多协议拉取（http/https/ftp/smb）：统一委托 TransferService（v2.7 模块化）。"""
        if self.transfer is None:
            raise RuntimeError("transfer service not wired")
        return await self.transfer.download_to(url, dest)

    async def _do_fetch(self, op) -> None:
        url = op.payload["url"]
        name = op.payload.get("name") or Path(urlsplit(url).path).name or "fetched"
        to_album = bool(op.payload.get("to_album"))
        to_essence = bool(op.payload.get("to_essence"))
        convert_to = str(op.payload.get("convert_to") or "")
        lossy = bool(op.payload.get("lossy"))
        staged = self.tmp_dir / f"fetch_{uuid.uuid4().hex[:10]}.tmp"
        try:
            size = await self._download(url, staged)
            if size <= 0:
                raise ValueError("fetched empty content")
            logger.info(f"[ingest] fetched {url} ({size} bytes)")

            # W2-B: optional format conversion before any target branch.
            if convert_to and self.converter is not None and not to_essence:
                downloaded = staged
                staged = await self.converter.convert(staged, convert_to)
                if downloaded != staged:
                    downloaded.unlink(missing_ok=True)
                if Path(name).suffix.lower() != convert_to:
                    name = f"{Path(name).stem}{convert_to}"

            # C-4: optional irreversible lossy re-encode for album media.
            if lossy and to_album and self.converter is not None:
                downloaded = staged
                staged = await self.converter.compress(staged)
                if downloaded != staged:
                    downloaded.unlink(missing_ok=True)
                name = f"{Path(name).stem}{staged.suffix}"

            if to_essence:
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
                    await self.api.upload_image_to_qun_album(
                        op.target, "", album_name, staged.as_posix()
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
                await self.api.upload_group_file(op.target, staged.as_posix(), name)
                lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
                result = await self.sync.run_full_sync(op.target, lock)
                if not result.ok:
                    logger.warning(f"[ingest] post-fetch sync failed: {result.error}")
                logger.info(f"[ingest] fetched -> file '{name}' in {op.target}")
        finally:
            staged.unlink(missing_ok=True)

    # ==================== 3. 长视频拆分存储 ====================

    async def submit_video_upload(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        folder_id: str | None = None,
    ) -> str:
        """视频入库：≤上限直接上传；超限 ffmpeg 分段（每段 ≤ 上限）逐段上传。"""
        return await self.queue.submit(
            "video_upload",
            target=group_id,
            payload={"path": staged_path, "name": name, "folder_id": folder_id or ""},
        )

    async def _download_video(self, url: str, dest: Path) -> None:
        """流式下载视频到本地（限时长/限大小；独立方法便于测试替换）。"""
        import httpx

        timeout = self.fetch_timeout
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > VIDEO_PREVIEW_MAX_BYTES:
                            raise ValueError("视频超过预览大小上限（300MB）")
                        f.write(chunk)

    async def _run_ffmpeg(self, args: list[str], timeout: int = 600) -> None:
        """ffmpeg 子进程（to_thread 防阻塞）；失败抛清晰错误。"""

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
        """均匀抽取 N 帧 → 两通道调色板 GIF（1 帧/秒循环）。"""
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
        """相册视频关键帧 GIF 预览（v2.5）。

        云端视频 URL → 下载（缓存 mp4）→ ffmpeg 均匀抽帧 → 调色板 GIF
        → 磁盘缓存（tmp/video_preview/<sha1>.gif）→ base64 供前端内联展示。
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
        for m in media or []:
            v = m.get("video") or {}
            if str(m.get("desc") or "") == name or str(v.get("name") or "") == name:
                entry = m
                break
        if entry is None and name == "":
            entry = next((m for m in (media or []) if m.get("video")), None)
        if entry is None:
            raise ValueError(f"视频条目不存在：{name or '(未命名)'}")

        v = entry.get("video") or {}
        raw = v.get("video_url")
        # 兼容双形态：NapCat 直接给 JSON 数组；个别协议端给字符串化 repr
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
        if not urls:
            raise ValueError("该视频云端未提供可下载地址（无法生成预览）")
        url = urls[0]["url"]["url"]
        duration_ms = int(v.get("video_time") or 0)

        vid = str(v.get("id") or entry.get("media_id") or name or url)
        cache_key = hashlib.sha1(f"{album_id}|{vid}".encode()).hexdigest()
        cache_dir = self.tmp_dir / "video_preview"
        cache_dir.mkdir(parents=True, exist_ok=True)
        gif_path = cache_dir / f"{cache_key}.gif"
        if not gif_path.exists():
            src = cache_dir / f"{cache_key}.mp4"
            if not src.exists():
                await self._download_video(url, src)
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
        """ffprobe 时长（秒）；不可探测返回 None（按直传处理）。"""
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

    async def _split_video(
        self, src: Path, out_dir: Path, max_sec: int, stem: str
    ) -> list[Path]:
        """强制关键帧分段（每段 ≤ max_sec；重编码保证切点精确）。"""
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video split")
        pattern = out_dir / f"{stem}_seg%03d.mp4"

        def _run():
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                src.as_posix(),
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{max_sec})",
                "-f",
                "segment",
                "-segment_time",
                str(max_sec),
                "-reset_timestamps",
                "1",
                pattern.as_posix(),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
            if proc.returncode != 0:
                raise ValueError(f"ffmpeg split failed: {proc.stderr[-300:]}")

        await asyncio.to_thread(_run)
        segments = sorted(out_dir.glob(f"{stem}_seg*.mp4"))
        if not segments:
            raise ValueError("ffmpeg produced no segments")
        return segments

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
        if dur is None or dur <= max_sec:
            await self.api.upload_group_file(op.target, path, name, folder_id=folder)
            lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
            result = await self.sync.run_full_sync(op.target, lock)
            if not result.ok:
                logger.warning(f"[ingest] post-video sync failed: {result.error}")
            logger.info(f"[ingest] video direct upload: {name} ({dur}s)")
            return
        # 长视频：拆分存储（单逻辑资源 + 分片 volumes）
        parent_id = f"vidgroup:{uuid.uuid4().hex[:10]}"
        parent_key = f"{op.target}:file:{parent_id}"
        await self.store.upsert_resources(
            [
                Resource(
                    group_id=op.target,
                    type=ResourceType.FILE,
                    name=name,
                    source_ref=parent_id,
                    size=size,
                    created_at=int(time.time()),
                    meta={"volumes": True, "kind": "video", "total_seconds": dur},
                )
            ]
        )
        seg_dir = self.tmp_dir / f"vid_{parent_id.split(':')[1]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segments = await self._split_video(src, seg_dir, max_sec, stem)
        total = len(segments)
        for seq, seg in enumerate(segments, 1):
            part_name = f"{stem}.part{seq:02d}.mp4"
            await self.api.upload_group_file(
                op.target, seg.as_posix(), part_name, folder_id=folder
            )
            sha = hashlib.sha256(seg.read_bytes()).hexdigest()
            await self.store.insert_volumes(
                [
                    VolumeInfo(
                        parent_resource_id=parent_key,
                        seq=seq,
                        part_name=part_name,
                        size=seg.stat().st_size,
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
        # 回填 source_ref/busid（upload_group_file 不返回 file_id）
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

    # ==================== 精华删除（分片级清理） ====================

    async def submit_essence_delete(self, group_id: str, id: int) -> str:
        """删除精华资源：逐分片移出精华 → 资源软删（云端为源，对账一致性）。"""
        row = await self.store.get_resource_any(id)
        if not row or str(row.get("group_id")) != str(group_id):
            raise ValueError(f"resource {id} not found in group {group_id}")
        if row.get("type") != "essence":
            raise ValueError("essence delete only accepts essence resources")
        return await self.queue.submit(
            "essence_delete",
            target=group_id,
            payload={
                "id": id,
                "parts": list((row.get("meta") or {}).get("parts") or []),
            },
        )

    async def _do_essence_delete(self, op) -> None:
        parts = op.payload.get("parts") or []
        failed = 0
        for p in parts:
            try:
                await self.api.delete_essence_msg(str(p.get("message_id") or ""))
            except Exception as e:
                failed += 1
                logger.warning(f"[ingest] essence part delete failed: {e}")
        # 云端可能已丢失该精华（会话句柄/手动移除）→ 本地软删保持一致性
        await self.store.update_resource_fields(op.payload["id"], status="deleted")
        logger.info(
            f"[ingest] essence deleted: {op.payload['id']} "
            f"({len(parts)} parts, {failed} cloud-miss)"
        )

    # ==================== op 分发 ====================

    async def submit_video_album(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        album_name: str = "AstrBot云盘",
    ) -> str:
        """化整为零（v2.8）：媒体文件分片导入群相册（每段 ≤ 时长上限）。"""
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
        """2026-09-01 N-06：单图导入群相册（upload_image_to_qun_album + 相册资源化刷新）。"""
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
        await self.api.upload_image_to_qun_album(
            op.target, "", op.payload["album_name"], src.as_posix()
        )
        # 相册资源化刷新（保留精华行：双类型全量重采该群）
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
        from core.composition.splitter import split_video

        src = Path(op.payload["path"])
        stem = Path(op.payload["name"]).stem or "video"
        seg_dir = self.tmp_dir / f"alb_{uuid.uuid4().hex[:10]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        try:
            segs = await split_video(src, seg_dir, stem, self.video_segment_seconds)
            total = len(segs)
            for i, seg in enumerate(segs, 1):
                await self.api.upload_image_to_qun_album(
                    op.target, "", op.payload["album_name"], seg.as_posix()
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
            # 相册资源化刷新（保留精华行）
            albums = await self.api.get_qun_album_list(op.target)
            essences = await self.api.get_essence_msg_list(op.target)
            await self.store.upsert_album_essence(op.target, albums, essences)
            logger.info(
                f"[ingest] media -> album '{op.payload['album_name']}' "
                f"({total} segments) in {op.target}"
            )
        finally:
            shutil.rmtree(seg_dir, ignore_errors=True)
        Path(op.payload["path"]).unlink(missing_ok=True)

    async def handle(self, op) -> None:
        if op.kind == "essence_save":
            await self._do_essence_save(op)
        elif op.kind == "essence_delete":
            await self._do_essence_delete(op)
        elif op.kind == "fetch":
            await self._do_fetch(op)
        elif op.kind == "video_upload":
            await self._do_video_upload(op)
        elif op.kind == "video_album":
            await self._do_video_album(op)
        elif op.kind == "image_album":
            # 2026-09-01 N-06：单图导入群相册
            await self._do_image_album(op)
        else:
            raise ValueError(f"unknown ingest op kind: {op.kind}")
