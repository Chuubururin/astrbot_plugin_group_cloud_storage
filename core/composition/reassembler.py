"""reassembler —— 化零为整：分卷拼接 / 视频 concat / 文本重建（含完整性校验）。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from core.composition.integrity import sha256_bytes, verify_part, verify_total


def reassemble_volumes(
    parts: list[dict], dest: str | Path, total_sha256: str | None = None
) -> str:
    """二进制分卷拼接：逐片 sha256 校验 → 顺序写入 → 整文件校验。

    parts: [{path|data?, sha256}]；返回整文件 sha256。
    """
    dest = Path(dest)
    with dest.open("wb") as of:
        for p in parts:
            data = (
                p["data"] if p.get("data") is not None else Path(p["path"]).read_bytes()
            )
            if not verify_part(data, p.get("sha256")):
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"part sha256 mismatch: {p.get('part_name', p.get('seq'))}"
                )
            of.write(data)
    if not verify_total(dest, total_sha256):
        dest.unlink(missing_ok=True)
        raise ValueError("total sha256 mismatch")
    return sha256_bytes(dest.read_bytes())


async def reassemble_video(seg_paths: list[str | Path], dest: str | Path) -> str:
    """视频分段重组：ffmpeg concat（流复制，无重编码）。"""
    dest = Path(dest)
    if not shutil.which("ffmpeg"):
        raise ValueError("ffmpeg not available for video reassemble")
    list_file = dest.parent / f"{dest.stem}_concat.txt"
    with list_file.open("w", encoding="utf-8") as lf:
        for seg in seg_paths:
            lf.write(f"file '{Path(seg).as_posix()}'\n")

    def _concat():
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file.as_posix(),
                "-c",
                "copy",
                dest.as_posix(),
            ],
            capture_output=True,
            text=True,
            timeout=14400,
        )
        if proc.returncode != 0:
            raise ValueError(f"ffmpeg concat failed: {proc.stderr[-300:]}")

    await asyncio.to_thread(_concat)
    list_file.unlink(missing_ok=True)
    return dest.as_posix()


def reassemble_text(parts: list[dict]) -> str:
    """文本分片重建：按 seq 排序拼接（本地缓存路径）；缺片抛出 ValueError。"""
    ordered = sorted(parts, key=lambda p: p.get("seq") or 0)
    texts = [p.get("text") for p in ordered]
    if not texts or any(t is None for t in texts):
        raise ValueError("text parts incomplete")
    return "\n".join(texts)
