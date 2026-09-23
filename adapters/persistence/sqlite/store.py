"""SqliteMetaStore -- MetaStorePort implementation.

Domain mixins are independent StorePart instances sharing connection,
path, and cache state via SharedState. The public interface is forwarded
via __getattr__ delegation (memoized after first access); MetaStorePort
conformance is structural (no Protocol base class).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import os
import sqlite3
import tempfile
import time
from typing import TYPE_CHECKING, Any

from core.log import logger

from .connection import ConnectionManager
from .state import SharedState, build_method_map
from .archive import ArchiveMixin
from .folders import FoldersMixin
from .groups import GroupsMixin
from .integrity import IntegrityMixin
from .migrations import SCHEMA_VERSION, migrate
from .netdisk import NetdiskMixin
from .outbox import OutboxMixin
from .resources_get import ResourceGetMixin
from .resources_query import ResourceQueryMixin
from .resources_write import ResourceWriteMixin
from .search import SearchMixin
from .scheduling import SchedulingMixin
from .sync import SyncMixin
from .volumes import VolumesMixin

# (attr name, Mixin class) -- __getattr__ forwards in exactly this order
_PARTS = (
    ("_resources_write", ResourceWriteMixin),
    ("_resources_query", ResourceQueryMixin),
    ("_resources_get", ResourceGetMixin),
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


def _rebuild_database(db_path: Path) -> None:
    """Build a fresh, migrated database next to ``db_path`` and swap it in.

    Blocking by design (sqlite3 plus filesystem); the caller runs it in a
    worker thread. When any step fails the temporary file is removed and the
    existing database file is left untouched, so a failed rebuild is a no-op.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{db_path.name}.", suffix=".rebuild", dir=db_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            conn.execute("BEGIN IMMEDIATE")
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
        os.replace(tmp, db_path)
    finally:
        if tmp.exists():
            tmp.unlink()


if TYPE_CHECKING:
    # Shadow declaration: runtime keeps composition + __getattr__ forwarding;
    # this branch only exists so IDE / pyright see all domain methods
    # (navigation, completion, rename propagation). The base list must stay in
    # sync with _PARTS -- pinned by test_shadow_bases_match_parts.
    class SqliteMetaStore(
        ResourceWriteMixin,
        ResourceQueryMixin,
        ResourceGetMixin,
        GroupsMixin,
        IntegrityMixin,
        VolumesMixin,
        FoldersMixin,
        SyncMixin,
        ArchiveMixin,
        SearchMixin,
        SchedulingMixin,
        OutboxMixin,
        NetdiskMixin,
    ):
        _METHOD_OWNER: dict[str, str]
        _state: SharedState
        _rebuild_lock: asyncio.Lock

        @property
        def _conn(self) -> ConnectionManager: ...
        @property
        def _db_path(self) -> Path: ...

        def iter_parts(self): ...
        async def init(self) -> None: ...
        async def reset_and_rebuild(self) -> None: ...
        async def close(self) -> None: ...
        def __getattr__(self, name: str) -> Any: ...
else:
    class SqliteMetaStore:
        """SQLite implementation of MetaStorePort (structurally conformant; does
        not inherit the Protocol base class -- otherwise stub methods would be
        hit before __getattr__ and return None).

        Composes the domain StoreParts; all DB access goes through the
        ConnectionManager persistent connection pool.
        """

        # Forwarded name -> part attr, built once at import; a duplicate name raises here.
        _METHOD_OWNER = build_method_map(_PARTS)

        def __init__(self, db_path: Path):
            self._state = SharedState(
                conn=ConnectionManager(db_path),
                db_path=Path(db_path),
            )
            for attr, part_cls in _PARTS:
                setattr(self, attr, part_cls(self._state))
            self._state.owner = self  # entry point for cross-part call resolution
            # Serializes reset_and_rebuild: two concurrent rebuilds would race on
            # close -> mkstemp -> os.replace and can corrupt the database file.
            self._rebuild_lock = asyncio.Lock()

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

            The name -> part map is static (`_METHOD_OWNER`), so a name claimed by
            two parts is an import-time error rather than a silent first-match-wins
            pick. Lookup uses the class (type(part)), never the instance
            __getattr__ chain, to prevent recursion.
            """
            if name.startswith("_"):
                raise AttributeError(name)
            attr = self._METHOD_OWNER.get(name)
            if attr is None:
                raise AttributeError(name)
            part = self.__dict__.get(attr)
            if part is None:
                raise AttributeError(name)
            value = getattr(part, name)
            self.__dict__[name] = value
            return value

        @property
        def _conn(self) -> ConnectionManager:
            return self._state.conn

        @property
        def _db_path(self) -> Path:
            return self._state.db_path

        async def init(self) -> None:
            def _do(conn):
                for attempt in range(3):
                    # IMMEDIATE: the migration is a write transaction, so take the
                    # write lock up front instead of risking a busy-snapshot
                    # failure when a deferred read upgrades to a write.
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Strict version chain: a failing migration statement
                        # aborts the chain, the transaction is rolled back and the
                        # error propagates, so the schema is never left
                        # half-migrated and the version marker only advances after
                        # a fully successful chain. The one best-effort block
                        # inside migrate() is the post-migration FTS repair, which
                        # cannot veto the version marker (it would otherwise make
                        # every later startup retry the same broken repair and
                        # never initialise the plugin).
                        migrate(conn)
                        conn.commit()
                        return
                    except sqlite3.OperationalError as e:
                        conn.rollback()
                        # Background op writers (WAL, pool of connections) can hold
                        # the write lock past busy_timeout; migrate is idempotent.
                        transient = "locked" in str(e) or "busy" in str(e)
                        if not transient or attempt == 2:
                            logger.error(
                                f"[group_cloud_storage] schema migration failed and was "
                                f"rolled back: {e}"
                            )
                            raise
                        time.sleep(1.5 * (attempt + 1))
                    except Exception as e:
                        conn.rollback()
                        logger.error(
                            f"[group_cloud_storage] schema migration failed and was "
                            f"rolled back: {e}"
                        )
                        raise

            await self._conn.exec(_do)
            # Refresh planner statistics once per startup. Without sqlite_stat1
            # the planner estimates from index cardinality alone and picks
            # idx_archive_map_state (two distinct values) over the primary key's
            # resource_id prefix for the correlated EXISTS in the store_status
            # queries, which turns them quadratic. PRAGMA optimize only runs
            # ANALYZE when SQLite judges the stats stale, so a steady-state
            # startup pays nothing.
            await self._conn.exec(lambda conn: conn.execute("PRAGMA optimize"))
            logger.info(
                f"[group_cloud_storage] meta.db ready (schema v{SCHEMA_VERSION}) at {self._db_path}"
            )

        async def reset_and_rebuild(self) -> None:
            """Atomically replace this database with an empty, validated schema."""
            async with self._rebuild_lock:
                await self._reset_and_rebuild_locked()

        async def _reset_and_rebuild_locked(self) -> None:
            try:
                await self.close()
                # Quiesce before os.replace: the swap unlinks the inode a call
                # that is still running holds, so its write would commit there
                # and vanish without an error.
                await self._state.conn.drain()
                # sqlite3 + filesystem work is blocking; keep it off the event
                # loop, like ConnectionManager.execute() and IntegrityMixin.
                await asyncio.to_thread(_rebuild_database, self._db_path)
            finally:
                # Rebuild the manager on the failure path too: close() above
                # retired the old one, so without this every later store call
                # would raise "connection manager is closed" forever. Same
                # contract as IntegrityMixin.restore().
                self._state.conn = ConnectionManager(self._db_path)

        async def close(self) -> None:
            await self._conn.close()
