"""reassembler — reassembly: volume concatenation / video concat / text
reconstruction (with integrity checks).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from core.application.composition.integrity import sha256_bytes, verify_part, verify_total


def reassemble_volumes(
    parts: list[dict], dest: str | Path, total_sha256: str | None = None
) -> str:
    """Binary volume concatenation: per-part sha256 verification -> sequential
    write -> whole-file verification.

    parts: [{path|data?, sha256}]; returns the whole-file sha256.
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
    """Video segment reassembly: ffmpeg concat (stream copy, no re-encoding)."""
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
    """Text chunk reconstruction: join parts sorted by seq (local cache
    paths); raises ValueError when a part is missing.
    """
    ordered = sorted(parts, key=lambda p: p.get("seq") or 0)
    texts = [p.get("text") for p in ordered]
    if not texts or any(t is None for t in texts):
        raise ValueError("text parts incomplete")
    return "\n".join(texts)
