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
        self._closed = False
        # Guards _created and the in-flight counter below.
        self._lock = threading.Lock()
        # Calls that already checked out a connection. close() retires the
        # pool but does NOT wait for them, so a swap-in-place (rebuild /
        # restore) must drain() first or a straggling write lands on the old
        # file and is lost silently.
        self._inflight = 0
        self._idle = threading.Event()
        self._idle.set()

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
        if self._closed:
            raise RuntimeError("connection manager is closed")
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._pool_size:
                self._created += 1
                # Reserve before connecting (the lock is held across the
                # blocking connect) but give the slot back when _connect()
                # fails: a permanent reservation leaked one unit of pool
                # capacity per failure, so after pool_size failures no new
                # connection could ever be created and every call blocked
                # for 30s before raising TimeoutError.
                try:
                    return self._connect()
                except BaseException:
                    self._created -= 1
                    raise
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
        # Counted before the checkout: a call that is still waiting for a
        # pooled connection must also block drain(), otherwise it can start
        # executing after the database file was swapped.
        with self._lock:
            self._inflight += 1
            self._idle.clear()
        conn = None
        try:
            conn = self._acquire()
            return fn(conn, *args)
        finally:
            if conn is not None:
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if self._closed:
                    # close() ran while this call was in flight: retire the
                    # connection instead of pooling it (nothing will drain it).
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._pool.put(conn)
            with self._lock:
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()

    async def execute(self, fn, *args):
        """Run blocking fn(conn, *args) on a pooled connection."""
        return await asyncio.to_thread(self._run, fn, *args)

    # Backward-compatible alias for execute().
    exec = execute

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait (bounded) for in-flight calls to return; True when idle.

        close() only retires the pool. A call that already holds a connection
        keeps running against the old handle, which matters whenever the
        database file is swapped underneath it (reset_and_rebuild's
        os.replace unlinks the old inode; restore() overwrites the live file
        in place). Call this between close() and the swap.

        Best-effort by design, like OpQueue.shutdown(): a call stuck waiting
        on a saturated pool can outlive the timeout, so this returns False
        instead of blocking the caller forever.
        """
        if self._inflight == 0:
            return True
        return await asyncio.to_thread(self._idle.wait, timeout)

    async def close(self) -> None:
        # Drain idle connections; the closed flag goes up first so checkouts
        # still in flight are retired by _run on return instead of being
        # pooled again (no connection outlives close()).
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                conn.close()
            except Exception:
                pass
