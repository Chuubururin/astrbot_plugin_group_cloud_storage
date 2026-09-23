"""Integrity -- database integrity checking.

Provides:
- IntegrityMixin.backup()/restore(): online SQLite backup API
- check_integrity(): run integrity checks on the database

(The reset/rebuild path lives in SqliteMetaStore.reset_and_rebuild.)
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from core.domain.enums import StoreUnavailable

from .connection import ConnectionManager
from .state import StorePart


class IntegrityMixin(StorePart):
    async def health_check(self):
        return {"ok": True, "backend": "sqlite"}

    async def integrity_check(self):
        return await check_integrity(self._db_path)

    async def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        def _copy():
            src = sqlite3.connect(str(self._db_path))
            dst = sqlite3.connect(str(destination))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        await asyncio.to_thread(_copy)
        return {"ok": True, "path": str(destination)}

    async def restore(self, source):
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        def _copy():
            src = sqlite3.connect(str(source))
            dst = sqlite3.connect(str(self._db_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        # Same pattern as reset_and_rebuild: retire pooled handles first so
        # no connection keeps stale page cache / WAL state across the
        # restore, then swap in a fresh manager (a closed manager refuses
        # new checkouts) -- on failure too, so the store stays usable.
        await self._conn.close()
        try:
            # Quiesce before overwriting the live file: a call still holding a
            # connection from the retired pool could commit on top of the
            # restored database and leave a mixed state. drain() is bounded, so
            # a False result means a straggler exists - swapping then would be
            # the exact corruption this guard exists to prevent, so refuse and
            # let the caller retry.
            if not await self._conn.drain():
                raise StoreUnavailable(
                    "restore aborted: database calls still in flight, retry shortly"
                )
            await asyncio.to_thread(_copy)
        finally:
            self._state.conn = ConnectionManager(self._db_path)
        return {"ok": True, "path": str(self._db_path)}


async def check_integrity(db_path: str | Path) -> dict:
    """Check SQLite database integrity.

    ``PRAGMA integrity_check`` scans the whole database (seconds on a large
    file), so the blocking work runs in a worker thread: the event loop keeps
    serving requests while the check is in flight, like backup()/restore().

    Returns:
        {"ok": bool, "errors": list[str], "warnings": list[str]}
    """
    return await asyncio.to_thread(_check_integrity_sync, Path(db_path))


def _check_integrity_sync(db_path: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if not db_path.exists():
        return {"ok": False, "errors": ["database file not found"], "warnings": []}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception as e:
        return {"ok": False, "errors": [f"cannot open database: {e}"], "warnings": []}

    try:
        # PRAGMA integrity_check
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            errors.append(f"integrity_check: {result[0]}")

        # Check FTS consistency
        try:
            conn.execute("SELECT * FROM resources_fts LIMIT 0")
        except Exception as e:
            warnings.append(f"FTS table issue: {e}")

        # Check schema version
        try:
            ver = conn.execute("SELECT version FROM schema_version").fetchone()
            if ver is None:
                warnings.append("schema_version table empty")
        except Exception as e:
            warnings.append(f"schema_version check: {e}")

    finally:
        conn.close()

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}

