"""reassembler — reassembly: volume concatenation / video concat / text
reconstruction (with integrity checks).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from core.application.composition.integrity import verify_part, verify_total

# Memory safety limit: reject reassembly of files larger than 4 GB to
# prevent memory exhaustion from loading entire files into RAM for SHA-256
# verification.
_MAX_REASSEMBLY_BYTES = 4 * 1024**3


def reassemble_volumes(
    parts: list[dict], dest: str | Path, total_sha256: str | None = None
) -> str:
    """Binary volume concatenation: per-part sha256 verification -> sequential
    write -> whole-file verification.

    parts: [{path|data?, sha256}]; returns the whole-file sha256.
    """
    dest = Path(dest)
    total_size = sum(
        len(p["data"]) if p.get("data") is not None else Path(p["path"]).stat().st_size
        for p in parts
    )
    if total_size > _MAX_REASSEMBLY_BYTES:
        raise ValueError(
            f"reassembled size {total_size} exceeds safety limit "
            f"({_MAX_REASSEMBLY_BYTES} bytes)"
        )
    # Stream into a temp file and publish it with one atomic rename: the
    # destination only ever appears complete and no partial file survives a
    # crash/mismatch. Nothing is accumulated in RAM — the previous version kept
    # every 64KB chunk in a list (chunks_buf), i.e. the whole volume in memory,
    # which defeated the 4GB ceiling above.
    tmp = dest.parent / f".{dest.name}.reassembling"
    try:
        with tmp.open("wb") as of:
            for p in parts:
                if p.get("data") is not None:
                    # In-memory data: verify and write directly
                    data = p["data"]
                    if not verify_part(data, p.get("sha256")):
                        raise ValueError(
                            f"part sha256 mismatch: {p.get('part_name', p.get('seq'))}"
                        )
                    of.write(data)
                else:
                    # BUG-7 fix: chunked read-verify-and-write. The full
                    # SHA-256 is accumulated while streaming 64KB chunks into
                    # the temp file; a mismatch aborts before the rename, so
                    # the destination is never partially written.
                    part_path = Path(p["path"])
                    sha = hashlib.sha256()
                    with part_path.open("rb") as pf:
                        while chunk := pf.read(1 << 16):
                            sha.update(chunk)
                            of.write(chunk)
                    if p.get("sha256") and sha.hexdigest() != p["sha256"]:
                        raise ValueError(
                            f"part sha256 mismatch: {p.get('part_name', p.get('seq'))}"
                        )
        if not verify_total(tmp, total_sha256):
            raise ValueError("total sha256 mismatch")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # Stream hash instead of reading entire file into memory
    sha = hashlib.sha256()
    with dest.open("rb") as f:
        while chunk := f.read(1 << 16):
            sha.update(chunk)
    return sha.hexdigest()


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

    try:
        await asyncio.to_thread(_concat)
    finally:
        # The concat manifest must not survive a failed concat: the failure
        # path used to leave <dest>_concat.txt behind in the output dir.
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
