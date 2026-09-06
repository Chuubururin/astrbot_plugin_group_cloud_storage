"""Application-layer shared base functions (stateless, zero core.* deps).

Timestamp/path-name handling is unified here; domain-local aliases (e.g.
bridge._now) remain thin forwards to preserve the existing import surface.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Unified timestamp: UTC ISO-8601 (used for all task/op updated_at values)."""
    return datetime.now(timezone.utc).isoformat()


def path_basename(path: str) -> str:
    """Extract the file name from a remote/local path (tolerates trailing
    slashes and bare names without slashes).
    """
    return path.rstrip("/").rsplit("/", 1)[-1] if "/" in path else path


def split_ext(filename: str) -> tuple[str, str]:
    """Split into (base name, extension with dot); the extension is an empty string when absent."""
    if "." in filename:
        idx = filename.rfind(".")
        return filename[:idx], filename[idx:]
    return filename, ""


__all__ = ["utc_now_iso", "path_basename", "split_ext"]
