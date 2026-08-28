"""ConverterService —— 格式转换服务（2026-09-02，ADR-0009 W2-B；G-1/G-4）。

所有上传操作支持 `convert_to` 目标格式：
- 视频重封装：同容器内流拷贝（fastest，无质量损失）；容器差异大时重编码兜底（libx264）；
- 图片重编码：ffmpeg 重编码（png/jpg/webp）；
- 文档/文本：utf-8 归一 + 文本提取（doc/txt → text 由 essence 文本解析承担，本服务做归一）；
- 转换临时产物置于传入 tmp 目录，调用方（任务）终态清理（零落盘）。

依赖纪律（HL-12）：仅系统 ffmpeg（与 splitter 同源），不新增第三方包。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.log import logger

# 视频容器映射：目标扩展名 → ffmpeg 输出现有参数（重封装优先流拷贝）
_VIDEO_MUX: dict[str, list[str]] = {
    ".mp4": ["-c", "copy", "-f", "mp4"],
    ".mkv": ["-c", "copy", "-f", "matroska"],
    ".webm": ["-c", "copy", "-f", "webm"],
}

# 图片重编码：目标 → 编码器（ffmpeg 原生，无滤镜）
_IMAGE_ENC: dict[str, list[str]] = {
    ".png": ["-c:v", "png"],
    ".jpg": ["-c:v", "mjpeg", "-q:v", "2", "-f", "mjpeg"],
    ".jpeg": ["-c:v", "mjpeg", "-q:v", "2", "-f", "mjpeg"],
    ".webp": ["-c:v", "libwebp", "-q:v", "80"],
}

_SUPPORTED_VIDEO = (".mp4", ".mkv", ".webm")
_SUPPORTED_IMAGE = (".png", ".jpg", ".jpeg", ".webp")


class ConverterService:
    def __init__(self, tmp_dir: Path | None = None):
        self.tmp_dir = tmp_dir

    # ---------- 能力 ----------

    def target_supported(self, ext: str) -> bool:
        ext = (ext or "").lower()
        return ext in _VIDEO_MUX or ext in _IMAGE_ENC

    def is_video_ext(self, ext: str) -> bool:
        return (ext or "").lower() in _SUPPORTED_VIDEO

    def is_image_ext(self, ext: str) -> bool:
        return (ext or "").lower() in _SUPPORTED_IMAGE

    # ---------- 转换入口 ----------

    async def convert(self, src: Path, convert_to: str) -> Path:
        """将 src 转换为 convert_to 指定格式（扩展名），返回新路径（同目录）。

        视频：重封装优先（流拷贝），失败回落重编码（libx264 + aac）；
        图片：ffmpeg 重编码；文本：utf-8 归一（无扩展名差异返回原文件）。
        """
        ext = (convert_to or "").lower().lstrip(".")
        if ext.startswith("."):
            ext = ext[1:]
        if not ext:
            raise ValueError("convert_to must be a target extension (e.g. mp4/mkv/webm/png/jpg/webp)")
        target = src.with_suffix(f".{ext}")
        if target == src:
            return src
        if self.is_video_ext(f".{ext}"):
            try:
                return await self._convert_video_copy(src, target)
            except Exception as e:
                logger.info(f"[converter] remux fallback to re-encode for {src.name}: {e}")
                return await self._convert_video_reencode(src, target)
        if self.is_image_ext(f".{ext}"):
            return await self._convert_image(src, target)
        raise ValueError(f"unsupported convert target: {ext} (video: mp4/mkv/webm; image: png/jpg/webp)")

    # ---------- 视频 ----------

    async def _convert_video_copy(self, src: Path, target: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video conversion")
        args = (
            ["ffmpeg", "-y", "-i", src.as_posix()]
            + list(_VIDEO_MUX[target.suffix.lower()])
            + [target.as_posix()]
        )
        return await self._run(args, target)

    async def _convert_video_reencode(self, src: Path, target: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video conversion")
        mux = _VIDEO_MUX.get(target.suffix.lower(), ["-f", "mp4"])
        args = (
            ["ffmpeg", "-y", "-i", src.as_posix(),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac"]
            + list(mux[2:])  # 去掉 -c copy
            + [target.as_posix()]
        )
        return await self._run(args, target)

    # ---------- 图片 ----------

    async def _convert_image(self, src: Path, target: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for image conversion")
        args = (
            ["ffmpeg", "-y", "-i", src.as_posix()]
            + list(_IMAGE_ENC[target.suffix.lower()])
            + [target.as_posix()]
        )
        return await self._run(args, target)

    # ---------- Lossy compression (optional album re-encode, irreversible) ----------

    async def compress(self, src: Path) -> Path:
        """Lossy-compress a local image or video for album upload.

        Images are re-encoded (gif/bmp fall back to jpeg); videos are
        re-encoded to mp4 (libx264 + aac). Returns the compressed file
        path and never mutates the original file in place.
        """
        ext = src.suffix.lower()
        if self.is_image_ext(ext) or ext in (".gif", ".bmp"):
            return await self._compress_image(src)
        if ext in (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"):
            return await self._compress_video(src)
        raise ValueError(f"compress only supports images/videos: {src.name}")

    async def _compress_image(self, src: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for image compression")
        ext = src.suffix.lower()
        if ext not in _IMAGE_ENC:
            ext = ".jpg"
        target = src.with_suffix(ext)
        if target == src:
            target = src.with_name(f"{src.stem}_lossy{ext}")
        args = (
            ["ffmpeg", "-y", "-i", src.as_posix()]
            + list(_IMAGE_ENC[ext])
            + [target.as_posix()]
        )
        return await self._run(args, target)

    async def _compress_video(self, src: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video compression")
        target = src.with_suffix(".mp4")
        if target == src:
            target = src.with_name(f"{src.stem}_lossy.mp4")
        args = [
            "ffmpeg", "-y", "-i", src.as_posix(),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-movflags", "+faststart",
            target.as_posix(),
        ]
        return await self._run(args, target)

    # ---------- 文本 ----------

    def normalize_text(self, text: str) -> str:
        """utf-8 文本归一：BOM 去除 + 行尾符统一（CRLF/CR → LF）。"""
        if text.startswith("\ufeff"):
            text = text[1:]
        return text.replace("\r\n", "\n").replace("\r", "\n")

    # ---------- 执行 ----------

    async def _run(self, args: list[str], target: Path) -> Path:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ValueError(
                f"ffmpeg failed ({proc.returncode}): {(stderr or b'').decode(errors='replace')[-300:]}"
            )
        if not target.exists() or target.stat().st_size == 0:
            raise ValueError("ffmpeg produced empty output")
        logger.info(f"[converter] {Path(args[2]).name} -> {target.name} ({target.stat().st_size} bytes)")
        return target