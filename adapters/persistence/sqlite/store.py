"""SqliteMetaStore -- MetaStorePort implementation.

Domain mixins are independent StorePart instances sharing connection,
path, and cache state via SharedState. The public interface is forwarded
via __getattr__ delegation (memoized after first access); MetaStorePort
conformance is structural (no Protocol base class).
"""
from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import tempfile

from core.log import logger
from ports.meta_store import MetaStorePort

from .connection import ConnectionManager
from .state import SharedState
from .archive import ArchiveMixin
from .folders import FoldersMixin
from .groups import GroupsMixin
from .integrity import IntegrityMixin
from .migrations import SCHEMA_VERSION, migrate
from .netdisk import NetdiskMixin
from .outbox import OutboxMixin
from .resources import ResourcesMixin
from .search import SearchMixin
from .scheduling import SchedulingMixin
from .sync import SyncMixin
from .volumes import VolumesMixin

# (attr name, Mixin class) -- __getattr__ forwards in exactly this order
_PARTS = (
    ("_resources", ResourcesMixin),
    ("_groups", GroupsMixin),
    ("_integrity", IntegrityMixin),
    ("_volumes", VolumesMixin),
    ("_folders", FoldersMixin),
    ("_sync", SyncMixin),
    ("_archive", ArchiveMixin),
    ("_search", SearchMixin),
    ("_scheduling", SchedulingMixin),
    ("_outbox", OutboxMixin),
    ("_netdisk", NetdiskMixin),
)


class SqliteMetaStore:
    """SQLite implementation of MetaStorePort (structurally conformant; does
    not inherit the Protocol base class -- otherwise stub methods would be
    hit before __getattr__ and return None).

    Composes the domain StoreParts; all DB access goes through the
    ConnectionManager persistent connection pool.
    """

    def __init__(self, db_path: Path):
        self._state = SharedState(
            conn=ConnectionManager(db_path),
            db_path=Path(db_path),
        )
        for attr, part_cls in _PARTS:
            setattr(self, attr, part_cls(self._state))
        self._state.owner = self  # entry point for cross-part call resolution

    def iter_parts(self):
        """Yield (attr name, part instance) in declaration order, used for
        cross-part resolution."""
        for attr, _part_cls in _PARTS:
            part = self.__dict__.get(attr)
            if part is not None:
                yield attr, part

    def __getattr__(self, name: str):
        """Forward domain methods to the composed StoreParts (memoized into
        the instance dict after first access).

        Existence checks use the class (type(part)), not the instance
        __getattr__ chain, to prevent recursion.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        for _attr, part in self.iter_parts():
            if hasattr(type(part), name):
                value = getattr(part, name)
                self.__dict__[name] = value
                return value
        raise AttributeError(name)

    @property
    def _conn(self) -> ConnectionManager:
        return self._state.conn

    @property
    def _db_path(self) -> Path:
        return self._state.db_path

    async def init(self) -> None:
        def _do(conn):
            conn.execute("BEGIN")
            try:
                migrate(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)
        logger.info(
            f"[group_cloud_storage] meta.db ready (schema v{SCHEMA_VERSION}) at {self._db_path}"
        )

    async def reset_and_rebuild(self) -> None:
        """Atomically replace this database with an empty, validated schema."""
        await self.close()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self._db_path.name}.", suffix=".rebuild", dir=self._db_path.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            conn = sqlite3.connect(str(tmp))
            try:
                conn.execute("BEGIN")
                migrate(conn)
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError(f"rebuilt database integrity check failed: {check}")
                conn.commit()
            finally:
                conn.close()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{tmp}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(tmp, self._db_path)
            self._state.conn = ConnectionManager(self._db_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    async def close(self) -> None:
        await self._conn.close()
