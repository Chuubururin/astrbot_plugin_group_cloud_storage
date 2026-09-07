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


async def compute_capacity(api, store, group_id: str) -> tuple[int, int, int, int] | None:
    """Unified capacity policy (cloud first, local index fallback).

    fs success with total>0 -> returns the 4-tuple (used/count fall back to
    field-level local index aggregates when the fs fields are zero).
    fs failure or total=0 -> returns None (the caller skips the capacity
    write and keeps the last known good values, preventing 0-value churn).

    Shared by the operation queue (refresh_capacity) and the group scanner
    (_capacity_of); both previously carried a copy of this policy.
    """
    try:
        fs = await api.get_group_fs_info(group_id)
    except Exception:
        return None
    if not fs.total_space:
        return None
    used = fs.used_space or await store.sum_resource_sizes(group_id)
    count = fs.file_count or await store.count_active(group_id)
    return used, fs.total_space, count, fs.limit_count


__all__ = ["utc_now_iso", "path_basename", "split_ext", "compute_capacity"]
