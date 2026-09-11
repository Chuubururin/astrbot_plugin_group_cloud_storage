"""ConverterService — format conversion service.

All upload operations support a `convert_to` target format:
- Video remux: stream copy within the same container (fastest, no quality loss);
  falls back to re-encoding (libx264) when containers differ too much;
- Image re-encode: ffmpeg re-encoding (png/jpg/webp);
- Documents/text: utf-8 normalization + text extraction (doc/txt -> text parsing is
  handled by the essence text parser; this service only normalizes);
- Conversion temp outputs go into the provided tmp directory; the caller (task)
  cleans them up when it reaches a terminal state.

Dependency discipline: system ffmpeg only (same source as splitter), no new third-party packages.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.log import logger

# Video container mapping: target extension -> ffmpeg output args (remux prefers stream copy)
_VIDEO_MUX: dict[str, list[str]] = {
    ".mp4": ["-c", "copy", "-f", "mp4"],
    ".mkv": ["-c", "copy", "-f", "matroska"],
    ".webm": ["-c", "copy", "-f", "webm"],
}

# Image re-encoding: target -> encoder (native ffmpeg, no filters)
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

    # ---------- Capabilities ----------

    def target_supported(self, ext: str) -> bool:
        ext = (ext or "").lower()
        return ext in _VIDEO_MUX or ext in _IMAGE_ENC

    def is_video_ext(self, ext: str) -> bool:
        return (ext or "").lower() in _SUPPORTED_VIDEO

    def is_image_ext(self, ext: str) -> bool:
        return (ext or "").lower() in _SUPPORTED_IMAGE

    def is_media_ext(self, ext: str) -> bool:
        """True when compress() can re-encode this extension (images incl.
        gif/bmp fallbacks, or any supported video container)."""
        ext = (ext or "").lower()
        return (
            ext in _SUPPORTED_IMAGE
            or ext in (".gif", ".bmp")
            or ext in (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv")
        )

    # ---------- Conversion entry points ----------

    async def convert(self, src: Path, convert_to: str) -> Path:
        """Convert src to the format named by convert_to (extension), returning the new path (same directory).

        Video: remux preferred (stream copy), falling back to re-encode (libx264 + aac) on failure;
        Image: ffmpeg re-encode; text: utf-8 normalization (returns the original file when
        the target extension matches).
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

    # ---------- Video ----------

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
            + list(mux[2:])  # drop -c copy
            + [target.as_posix()]
        )
        return await self._run(args, target)

    # ---------- Image ----------

    async def _convert_image(self, src: Path, target: Path) -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for image conversion")
        args = (
            ["ffmpeg", "-y", "-i", src.as_posix()]
            + list(_IMAGE_ENC[target.suffix.lower()])
            + [target.as_posix()]
        )
        return await self._run(args, target)

    # ---------- Lossy compression (user-selected album re-encode, irreversible) ----------

    # Quality tiers: level -> (video crf, jpeg/mjpeg q:v, webp q:v)
    _LOSSY_LEVELS: dict[str, tuple[int, int, int]] = {
        "high": (23, 2, 90),  # light compression, best quality
        "medium": (28, 5, 80),
        "low": (33, 8, 65),  # strongest compression
    }

    async def compress(self, src: Path, level: str = "medium", src_ext: str = "") -> Path:
        """Lossy-compress a local image or video for album upload.

        Images are re-encoded (gif/bmp fall back to jpeg); videos are
        re-encoded to mp4 (libx264 + aac). The tier (high/medium/low) is the
        user's per-upload choice. Returns the compressed file path and never
        mutates the original file in place. ``src_ext`` overrides the type
        detection for neutrally-suffixed staging files (e.g. .tmp); it must
        name a supported media extension.
        """
        level = str(level or "medium").lower()
        if level not in self._LOSSY_LEVELS:
            level = "medium"
        ext = (src_ext or src.suffix).lower()
        if self.is_image_ext(ext) or ext in (".gif", ".bmp"):
            return await self._compress_image(src, level, ext)
        if ext in (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"):
            return await self._compress_video(src, level, ext)
        raise ValueError(f"compress only supports images/videos: {src.name}")

    async def _compress_image(self, src: Path, level: str = "medium", src_ext: str = "") -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for image compression")
        crf, q_jpeg, q_webp = self._LOSSY_LEVELS[level]
        ext = (src_ext or src.suffix).lower()
        if ext == ".webp":
            args = ["ffmpeg", "-y", "-i", src.as_posix(),
                    "-c:v", "libwebp", "-q:v", str(q_webp)]
            target = src.with_suffix(".webp")
        elif ext in (".jpg", ".jpeg", ".gif", ".bmp", ""):
            args = ["ffmpeg", "-y", "-i", src.as_posix(),
                    "-c:v", "mjpeg", "-q:v", str(q_jpeg), "-f", "mjpeg"]
            target = src.with_suffix(".jpg")
        else:  # png: lossless codec, only strip/re-encode container
            args = ["ffmpeg", "-y", "-i", src.as_posix(), "-c:v", "png"]
            target = src.with_suffix(".png")
        if target == src:
            target = src.with_name(f"{src.stem}_lossy{target.suffix}")
        return await self._run(args + [target.as_posix()], target)

    async def _compress_video(self, src: Path, level: str = "medium", src_ext: str = "") -> Path:
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video compression")
        crf, _q_jpeg, _q_webp = self._LOSSY_LEVELS[level]
        target = src.with_suffix((src_ext or src.suffix).lower())
        if target == src:
            target = src.with_name(f"{src.stem}_lossy.mp4")
        args = [
            "ffmpeg", "-y", "-i", src.as_posix(),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-c:a", "aac", "-movflags", "+faststart",
            target.as_posix(),
        ]
        return await self._run(args, target)

    # ---------- Text ----------

    def normalize_text(self, text: str) -> str:
        """Normalize text to utf-8: strip the BOM and unify line endings (CRLF/CR -> LF)."""
        if text.startswith("\ufeff"):
            text = text[1:]
        return text.replace("\r\n", "\n").replace("\r", "\n")

    # ---------- Execution ----------

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