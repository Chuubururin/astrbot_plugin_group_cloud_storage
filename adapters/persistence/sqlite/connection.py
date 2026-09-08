"""SQLite connection factory and executor.

Single responsibility: maintain a pool of long-lived connections and
execute blocking callables in a thread pool. Domain modules must NOT set
PRAGMA or create locks.

Connections are persistent: at most pool_size are created, PRAGMAs run
once per connection at creation time, and each call only checks out,
executes, and returns a connection. Concurrent reads are safe under WAL
mode; on return, any still-open transaction is rolled back so half-open
transactions cannot leak across calls.
"""
from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
from pathlib import Path


class ConnectionManager:
    """Pool of persistent WAL connections; exclusive checkout per call."""

    def __init__(self, db_path: Path, pool_size: int = 4):
        self._db_path = Path(db_path)
        self._pool_size = max(1, pool_size)
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()  # only guards the _created counter

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _acquire(self) -> sqlite3.Connection:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._pool_size:
                self._created += 1
                return self._connect()
        # BUG-11 fix: timeout prevents permanent hang when pool is exhausted
        # (e.g. all connections stuck in long-running FTS queries).
        try:
            return self._pool.get(timeout=30.0)
        except queue.Empty as exc:
            raise TimeoutError(
                f"connection pool exhausted (size={self._pool_size}); "
                "all connections are in use"
            ) from exc

    def _run(self, fn, *args):
        conn = self._acquire()
        try:
            return fn(conn, *args)
        finally:
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception:
                    pass
            self._pool.put(conn)

    async def execute(self, fn, *args):
        """Run blocking fn(conn, *args) on a pooled connection."""
        return await asyncio.to_thread(self._run, fn, *args)

    # Backward-compatible alias for execute().
    exec = execute

    async def close(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                conn.close()
            except Exception:
                pass
