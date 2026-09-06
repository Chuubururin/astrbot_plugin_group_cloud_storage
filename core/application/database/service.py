"""Database administration use cases for the embedded SQLite backend."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import asyncio
import sqlite3

class DatabaseAdminService:
    """Small application boundary around SQLite maintenance operations."""
    def __init__(self, store, data_dir: Path | None = None, token: str = ""):
        self.store = store
        self.data_dir = Path(data_dir) if data_dir else None
        self.token = token or ""

    def authorize(self, provided: str | None) -> bool:
        """Use the authenticated Page session when no extra token is configured."""
        if not self.token:
            return True
        import hmac
        return hmac.compare_digest(str(provided or ""), self.token)

    def _path(self, value: str | None, *, default: Path | None = None) -> Path:
        """Resolve maintenance paths inside the configured data directory."""
        if not self.data_dir:
            raise RuntimeError("database data directory unavailable")
        root = self.data_dir.resolve()
        path = (root / value).resolve() if value else (default or root)
        if path != root and root not in path.parents:
            raise ValueError("path must be inside database data directory")
        return path

    async def health(self) -> dict[str, Any]:
        checker = getattr(self.store, "health_check", None)
        return await checker() if checker else {"ok": True, "backend": "sqlite"}

    async def integrity(self) -> dict[str, Any]:
        checker = getattr(self.store, "integrity_check", None)
        return await checker() if checker else {"ok": True}

    async def backups(self) -> list[dict[str, Any]]:
        if not self.data_dir:
            return []
        root = self._path("backups")
        if not root.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size, "path": str(p)} for p in sorted(root.glob("*.db"))]

    async def backup(self, destination: str | None = None):
        target = self._path(destination, default=self._path("backups") / "meta.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        return await self.store.backup(target)

    async def restore(self, source: str):
        src = self._path(source)
        if not src.is_file():
            raise FileNotFoundError(src)
        # Validate before touching the live store, then use the store's atomic path.
        def check():
            with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as conn:
                return conn.execute("PRAGMA integrity_check").fetchone()[0]
        if await asyncio.to_thread(check) != "ok":
            raise ValueError("source database failed integrity check")
        return await self.store.restore(src)

    async def reset(self):
        """Rebuild an empty schema; store implementation provides rollback."""
        reset = getattr(self.store, "reset_and_rebuild", None)
        if reset is None:
            raise RuntimeError("database reset unavailable")
        await reset()
        return {"ok": True, "action": "reset"}
