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
        await asyncio.to_thread(_copy)
        return {"ok": True, "path": str(self._db_path)}


async def check_integrity(db_path: str | Path) -> dict:
    """Check SQLite database integrity.

    Returns:
        {"ok": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    db_path = Path(db_path)

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

