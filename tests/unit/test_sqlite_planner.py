"""Planner statistics for the archive_map cross-reference.

The store_status filters run a correlated EXISTS against archive_map. With no
sqlite_stat1 the planner judges `state` by its two distinct values and serves
the subquery from idx_archive_map_state instead of the primary key's
resource_id prefix: every outer row scans a large slice of the table. Measured
on 200k rows, 268 ms without statistics and 0.1 ms with them.

What the planner needs from ANALYZE is the average rows per key - one row per
resource_id beats hundreds per state value - so a row count that is directionally
right is enough, and these assertions hold whatever the version's cost model
puts the tipping point at. Pinning the tipping point itself does not: CI
bundles an older libsqlite than a dev machine, and a small uniform fixture
flips sides between the two.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite.resources_query import (  # noqa: E402
    ARCHIVE_MAP_OUT_DONE,
)
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402

ROWS = 1000

# The real filter fragment, so this pins the query the store actually runs.
QUERY = f"SELECT resources.id FROM resources WHERE EXISTS ({ARCHIVE_MAP_OUT_DONE})"


def _res(i: int) -> Resource:
    return Resource(
        group_id="g1", type=ResourceType.FILE, name=f"f{i}.zip", source_ref=f"ref_{i}",
        size=100, uploader_id="10001", uploader_name="Alice", busid=102,
        folder_id="dir1", folder_name="Docs", created_at=1700000000 + i,
    )


async def _fill(store: SqliteMetaStore) -> None:
    await store.upsert_resources([_res(i) for i in range(ROWS)])

    def _insert(conn):
        conn.executemany(
            "INSERT INTO archive_map "
            "(resource_id, group_id, task_id, remote_path, direction, state, updated_at) "
            "VALUES (?, 'g1', ?, ?, 'out', ?, '2026-01-01T00:00:00')",
            [(i + 1, f"t{i}", f"/remote/{i}",
              "pending" if i % 100 == 0 else "done") for i in range(ROWS)],
        )
        conn.commit()

    await store._conn.exec(_insert)


async def _archive_map_analyzed(store: SqliteMetaStore) -> bool:
    """Whether archive_map has its own sqlite_stat1 rows (the guard init uses)."""
    return bool(await store._conn.exec(lambda conn: conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1' LIMIT 1"
    ).fetchone() and conn.execute(
        "SELECT 1 FROM sqlite_stat1 WHERE tbl = 'archive_map' LIMIT 1"
    ).fetchone()))


async def _archive_access(store: SqliteMetaStore) -> str:
    """The archive_map line of the query plan."""
    plan = await store._conn.exec(
        lambda conn: [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {QUERY}")]
    )
    for line in plan:
        if "archive_map" in line or "am " in line:
            return line
    raise AssertionError(f"plan has no archive_map access: {plan}")


@pytest.mark.asyncio
async def test_init_analyzes_once_archive_map_has_rows(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()          # empty tables: ANALYZE has nothing to record
    await _fill(store)
    assert not await _archive_map_analyzed(store)
    # The failure this guards: state looks like the cheaper index to a planner
    # that has never seen this table.
    assert "idx_archive_map_state" in await _archive_access(store)

    await store.init()          # a startup that finds data to analyze
    assert await _archive_map_analyzed(store)
    access = await _archive_access(store)
    assert "sqlite_autoindex_archive_map_1" in access, access
    assert "resource_id=?" in access, access
    await store.close()


@pytest.mark.asyncio
async def test_statistics_survive_a_reopen(tmp_path):
    """sqlite_stat1 lives in the database file, so the good plan is what every
    later process gets - not per-connection state a pool checkout would lose."""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()
    await _fill(store)
    await store.init()
    assert await _archive_map_analyzed(store)
    await store.close()

    reopened = SqliteMetaStore(db)
    await reopened.init()
    assert await _archive_map_analyzed(reopened)
    assert "sqlite_autoindex_archive_map_1" in await _archive_access(reopened)
    await reopened.close()


@pytest.mark.asyncio
async def test_steady_state_startup_does_not_re_analyze(tmp_path):
    """The guard is what keeps this off the hot path: once archive_map has
    statistics, a restart leaves sqlite_stat1 byte-identical."""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()
    await _fill(store)
    await store.init()
    before = await store._conn.exec(lambda conn: conn.execute(
        "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx").fetchall())
    assert any(row[0] == "archive_map" for row in before)

    await store.init()
    after = await store._conn.exec(lambda conn: conn.execute(
        "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx").fetchall())
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    await store.close()
