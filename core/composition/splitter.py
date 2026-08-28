"""splitter —— 化整为零：大文件分卷 / 视频分段 / 文本分片。

- split_volume：二进制定长切分（WinRAR 风格 .partNN 命名），返回切片路径与逐片哈希
- split_video：ffmpeg 强制关键帧分段（每段 ≤ max_sec）
- split_text：长文本按行边界/句读回退硬切（每段 ≤ limit）
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from core.composition.integrity import sha256_bytes
from core.log import logger

SPLIT_VOLUME_BYTES = 95 * 1024 * 1024  # 分卷单卷上限（95MB，QQ 单文件直传上限内）

ESSENCE_CHUNK_MAX_CHARS = 4500
_PART_MARK = "[云盘|{title}|{seq}/{total}]"


def split_volume(
    src: str | Path,
    out_dir: str | Path,
    stem: str,
    part_bytes: int = SPLIT_VOLUME_BYTES,
) -> list[dict]:
    """二进制分卷：返回 [{seq, part_name, path, size, sha256}]（切片落盘）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    with Path(src).open("rb") as fh:
        seq = 1
        while True:
            chunk = fh.read(part_bytes)
            if not chunk:
                break
            part_name = f"{stem}.part{seq:02d}"
            (out_dir / part_name).write_bytes(chunk)
            parts.append(
                {
                    "seq": seq,
                    "part_name": part_name,
                    "path": out_dir / part_name,
                    "size": len(chunk),
                    "sha256": sha256_bytes(chunk),
                }
            )
            seq += 1
    return parts


def effective_chunk_limit(title: str, total: int, base: int) -> int:
    """分片正文上限 = 上限 - 分片标记开销（QQ 单条精华含标记 ≤ 上限，防截断）。"""
    marker = _PART_MARK.format(title=title, seq=total, total=total)
    return max(100, base - len(marker) - 2)


def split_text(text: str, limit: int = ESSENCE_CHUNK_MAX_CHARS) -> list[str]:
    """长文本拆分：优先按行边界切，超长单行在句读/空白边界回退硬切（每段 ≤ limit）。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 <= limit:
            buf = f"{buf}\n{line}" if buf else line
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        while len(line) > limit:
            cut = _best_cut(line, limit)
            chunks.append(line[:cut].rstrip())
            line = line[cut:].lstrip("\n")
        buf = line
    if buf:
        chunks.append(buf)
    return chunks


def _best_cut(line: str, limit: int) -> int:
    """在 limit 附近找最近的句读/空白边界（回退 90% 处硬切）。"""
    window = line[:limit]
    for ch in "。！？；\n  ，、":
        idx = window.rfind(ch)
        if idx >= int(limit * 0.9):
            return idx + 1
    return limit


async def split_video(
    src: str | Path, out_dir: str | Path, stem: str, max_sec: int
) -> list[Path]:
    """ffmpeg 强制关键帧分段（每段 ≤ max_sec；重编码保证切点精确）。"""
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
