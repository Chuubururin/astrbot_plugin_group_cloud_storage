"""Download-cache housekeeping: keep the mkdtemp cache root bounded.

``DownloadServerService`` serves cloud resources through a private
``tempfile.mkdtemp`` root, and for a long time the *only* thing that ever
removed bytes from it was the ``shutil.rmtree`` in ``shutdown()``.  A bot that
stays up for weeks therefore grew that directory until the next plugin reload --
one cached copy per distinct file ever opened, with no eviction at all.

Two independent rules, each switchable off with 0:

* TTL    (``cache_ttl_seconds``) -- drop entries untouched for that long;
* quota  (``cache_max_bytes``)   -- if the total still exceeds it, evict the
  least recently used entries until it fits.

Scope is deliberately local (per the maintenance plan): no file moves, no
shared cache abstraction, no background task -- the sweep runs on the cache
*miss* path, which is exactly when the directory is about to grow.

Fail-open by design: housekeeping must never break a download, so every
filesystem error is swallowed and the offending entry is simply skipped.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core.log import logger

# Never touch anything used this recently: a fresh cache file is very likely
# being streamed right now.  An open fd survives unlink on POSIX, but a file
# written seconds ago may still be mid-``os.replace`` (the writer stages into
# ``<name>.<hex>.part`` and renames).
CACHE_SWEEP_GRACE = 60.0
# Sweeps are triggered by cache misses; throttle them so a burst of downloads
# does not rescan the directory once per file.
CACHE_SWEEP_INTERVAL = 60.0
# maybe_sweep runs on the SFTP/transport threads, so the check-and-reserve of
# _cache_swept_at needs a guard: two concurrent misses used to both pass the
# throttle check and sweep at once.
_SWEEP_GUARD = threading.Lock()


def cache_entries(svc) -> list[tuple[float, int, Path]]:
    """``(last_use, size, path)`` for every cache file, oldest use first.

    Only regular files directly under the two cache dirs.  ``*.part`` staging
    files are skipped on purpose: their writer owns them (``finally:
    tmp.unlink``) and removing one mid-copy would truncate an in-flight
    download.
    """
    out: list[tuple[float, int, Path]] = []
    for base in (svc._cache_dir, svc._smb_dir):
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for path in children:
            if path.suffix == ".part" or not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            # atime is unreliable on noatime mounts; a cache file is written
            # exactly once, so mtime marks the same instant.  Take the newer.
            out.append((max(st.st_atime, st.st_mtime), st.st_size, path))
    out.sort(key=lambda entry: entry[0])
    return out


def sweep_cache(svc, *, now: float | None = None) -> int:
    """Drop idle / over-quota cache files; returns the bytes freed."""
    now = time.time() if now is None else now
    entries = cache_entries(svc)
    total = sum(size for _, size, _ in entries)
    ttl = int(getattr(svc, "cache_ttl_seconds", 0) or 0)
    quota = int(getattr(svc, "cache_max_bytes", 0) or 0)
    if ttl <= 0 and quota <= 0:
        return 0
    freed = 0
    for last_use, size, path in entries:  # oldest first
        if now - last_use < CACHE_SWEEP_GRACE:
            break  # the rest are at least as fresh
        idle = ttl > 0 and now - last_use >= ttl
        if not (idle or (quota > 0 and total > quota)):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
    return freed


def maybe_sweep(svc) -> None:
    """Sweep at most once per ``CACHE_SWEEP_INTERVAL`` (miss-path hook)."""
    now = time.time()
    with _SWEEP_GUARD:
        last = getattr(svc, "_cache_swept_at", None)
        if last is not None and now - last < CACHE_SWEEP_INTERVAL:
            return
        svc._cache_swept_at = now
    freed = sweep_cache(svc, now=now)
    if freed:
        logger.info(f"[dlserver] cache sweep freed {freed} bytes")
