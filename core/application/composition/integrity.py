"""integrity — integrity verification (per-part / whole-file SHA-256)."""

from __future__ import annotations

import hashlib
from pathlib import Path

# Single implementation lives in core.application.common (stateless, zero
# core.* deps); re-exported here so composition keeps its import surface.
from core.application.common import sha256_file  # noqa: F401


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_part(data: bytes, expected_sha: str | None) -> bool:
    """Per-part verification: a missing expected hash counts as passing
    (legacy-data compatibility).
    """
    return (not expected_sha) or sha256_bytes(data) == expected_sha


def verify_total(path: str | Path, expected_sha: str | None) -> bool:
    """Whole-file verification: a missing expected hash counts as passing."""
    return (not expected_sha) or sha256_file(path) == expected_sha
