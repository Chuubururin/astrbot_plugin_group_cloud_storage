"""Integrity -- database integrity checking and reset/rebuild.

Provides:
- check_integrity(): run integrity checks on the database
- reset_and_rebuild(): stop-write window + create temp empty db +
  integrity check + atomic replace
"""
from __future__ import annotations

import os
import sqlite3
import shutil
from pathlib import Path

from core.log import logger

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
                dst.close(); src.close()
        await __import__('asyncio').to_thread(_copy)
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
                dst.close(); src.close()
        await __import__('asyncio').to_thread(_copy)
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


async def reset_and_rebuild(
    db_path: str | Path,
    migrations: dict[int, list[str]],
    schema_version: int,
) -> bool:
    """Reset database: stop writes → create temp empty db → init schema → integrity check → atomic replace.

    Returns:
        True if rebuild succeeded
    """
    db_path = Path(db_path)
    backup_path = db_path.with_suffix(".db.bak")
    tmp_path = db_path.with_suffix(".db.tmp")

    logger.info(f"[group_cloud_storage] reset_and_rebuild: starting for {db_path}")

    try:
        # Step 1: Backup current database
        if db_path.exists():
            shutil.copy2(db_path, backup_path)
            logger.info(f"[group_cloud_storage] reset: backup created at {backup_path}")

        # Step 2: Create temporary empty database with full schema
        if tmp_path.exists():
            tmp_path.unlink()

        tmp_conn = sqlite3.connect(str(tmp_path))
        try:
            # Execute all migrations to build complete schema
            for v in sorted(migrations.keys()):
                for sql in migrations[v]:
                    tmp_conn.executescript(sql)
                tmp_conn.execute(
                    "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (v,)
                )
            tmp_conn.commit()

            # Step 3: Integrity check on new database
            result = tmp_conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] != "ok":
                raise RuntimeError(f"integrity check failed: {result[0]}")

            logger.info("[group_cloud_storage] reset: new database integrity OK")

        finally:
            tmp_conn.close()

        # Step 4: Atomic replace
        if os.name == "nt":
            # Windows: can't atomically rename over existing file
            if db_path.exists():
                db_path.unlink()
        tmp_path.rename(db_path)

        # Step 5: WAL sidecar cleanup
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_suffix(db_path.suffix + suffix)
            if sidecar.exists():
                sidecar.unlink()

        logger.info("[group_cloud_storage] reset_and_rebuild: completed successfully")
        return True

    except Exception as e:
        logger.error(f"[group_cloud_storage] reset_and_rebuild failed: {e}")
        # Rollback: restore from backup
        if backup_path.exists():
            if tmp_path.exists():
                tmp_path.unlink()
            shutil.copy2(backup_path, db_path)
            logger.info("[group_cloud_storage] reset: restored from backup")
        return False

    finally:
        # Cleanup backup
        if backup_path.exists():
            try:
                backup_path.unlink()
            except Exception:
                pass
