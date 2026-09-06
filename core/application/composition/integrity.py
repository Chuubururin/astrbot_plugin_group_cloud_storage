"""integrity — integrity verification (per-part / whole-file SHA-256)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Streaming whole-file hash (safe for large files)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_part(data: bytes, expected_sha: str | None) -> bool:
    """Per-part verification: a missing expected hash counts as passing
    (legacy-data compatibility).
    """
    return (not expected_sha) or sha256_bytes(data) == expected_sha


def verify_total(path: str | Path, expected_sha: str | None) -> bool:
    """Whole-file verification: a missing expected hash counts as passing."""
    return (not expected_sha) or sha256_file(path) == expected_sha
