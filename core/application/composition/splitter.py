"""splitter — split into volumes: large-file volumes / video segments / text chunks.

- split_video: lossless video segmentation via ffmpeg stream copy (-c copy,
  cut points snap to keyframes, target <= max_sec)
- split_text: long text split at line boundaries / sentence punctuation with
  a hard-cut fallback (each chunk <= limit)
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

ESSENCE_CHUNK_MAX_CHARS = 4500
_PART_MARK = "[云盘|{title}|{seq}/{total}]"


def effective_chunk_limit(title: str, total: int, base: int) -> int:
    """Chunk body limit = limit - part-marker overhead (a single QQ essence
    message including the marker stays <= limit, preventing truncation).
    """
    marker = _PART_MARK.format(title=title, seq=total, total=total)
    return max(100, base - len(marker) - 2)


# Structural cut points (priority over plain lines): markdown headings and
# Chinese ordinals (一、/（一）/1. style section headers)
_HEADING_RE = re.compile(r"^#{1,6}\s")
_ORDINAL_RE = re.compile(r"^(?:[一二三四五六七八九十百]{1,6}|[（(][一二三四五六七八九十百]{1,6}[)）]|\d{1,3})[、.．::]")


def split_text(text: str, limit: int = ESSENCE_CHUNK_MAX_CHARS) -> list[str]:
    """Long text splitting: prefer structural boundaries (headings / Chinese
    ordinals), then line (paragraph) boundaries; overlong single lines fall
    back to hard cuts at sentence-punctuation/whitespace boundaries (each
    chunk <= limit).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""

    def _flush():
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    def _push_line(line: str):
        nonlocal buf
        # A structural block (heading / ordinal) always starts a fresh chunk
        # so a section header never trails the previous section's tail
        if buf and (_HEADING_RE.match(line) or _ORDINAL_RE.match(line)):
            _flush()
            buf = line
            return
        if len(buf) + len(line) + 1 <= limit:
            buf = f"{buf}\n{line}" if buf else line
            return
        if buf:
            _flush()
        while len(line) > limit:
            cut = _best_cut(line, limit)
            chunks.append(line[:cut].rstrip())
            line = line[cut:].lstrip("\n")
        buf = line

    for line in text.split("\n"):
        _push_line(line)
    _flush()
    return chunks


def _best_cut(line: str, limit: int) -> int:
    """Find the nearest sentence-punctuation/whitespace boundary near limit
    (falls back to a hard cut at 90%).
    """
    window = line[:limit]
    for ch in "。！？；\n  ，、":
        idx = window.rfind(ch)
        if idx >= int(limit * 0.9):
            return idx + 1
    return limit


async def split_video(
    src: str | Path, out_dir: str | Path, stem: str, max_sec: int
) -> list[Path]:
    """Lossless video segmentation (ffmpeg -c copy stream copy, each segment
    targeted <= max_sec).

    Lossless cutting snaps cut points to keyframes: segment duration targets
    max_sec and may vary slightly when the keyframe interval is large (no
    re-encoding, zero quality loss). Group files have no per-segment duration
    cap, so the drift is harmless; 599s is a hard cap for album videos — album
    video upload is currently a reserved framework (the protocol side does not
    support it yet); when enabled, media transcoding (converter.compress) can
    serve as the lossy fallback.
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise ValueError("ffmpeg not available for video split")
    pattern = out_dir / f"{stem}_seg%03d.mp4"

    def _run():
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src.as_posix(),
                "-map",
                "0",
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(max_sec),
                "-reset_timestamps",
                "1",
                pattern.as_posix(),
            ],
            capture_output=True,
            text=True,
            timeout=14400,
        )
        if proc.returncode != 0:
            raise ValueError(f"ffmpeg split failed: {proc.stderr[-300:]}")

    await asyncio.to_thread(_run)
    segs = sorted(out_dir.glob(f"{stem}_seg*.mp4"))
    if not segs:
        raise ValueError("ffmpeg produced no segments")
    return segs
