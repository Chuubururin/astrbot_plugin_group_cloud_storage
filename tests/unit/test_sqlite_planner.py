"""Planner statistics for the archive_map cross-reference.

The store_status filters run a correlated EXISTS against archive_map. `state`
has two distinct values, so with no sqlite_stat1 the planner estimates from
index cardinality alone and serves the subquery from idx_archive_map_state
instead of the primary key's resource_id prefix: every outer row scans the
whole 'done' set. Measured on 200k rows, 268 ms without statistics and 0.1 ms
with them.
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

ROWS = 300

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
            [(i + 1, f"t{i}", f"/remote/{i}", "done" if i % 2 else "pending")
             for i in range(ROWS)],
        )
        conn.commit()

    await store._conn.exec(_insert)


async def _archive_access(store: SqliteMetaStore) -> str:
    """The archive_map line of the query plan."""
    plan = await store._conn.exec(
        lambda conn: [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {QUERY}")]
    )
    for line in plan:
        if "archive_map" in line or " am " in line:
            return line
    raise AssertionError(f"plan has no archive_map access: {plan}")


@pytest.mark.asyncio
async def test_init_writes_the_statistics_the_plan_needs(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()          # empty schema: nothing to analyze yet
    await _fill(store)

    # The failure this guards: state is the cheaper-looking index, and the
    # planner has no row counts to tell it otherwise.
    assert "idx_archive_map_state" in await _archive_access(store)

    await store.init()          # a startup after data accumulated
    access = await _archive_access(store)
    assert "sqlite_autoindex_archive_map_1" in access, access
    assert "resource_id=?" in access, access
    await store.close()


@pytest.mark.asyncio
async def test_statistics_survive_a_reopen(tmp_path):
    """sqlite_stat1 is persistent, so the good plan is what every later process
    gets - not a per-connection setting that a pool checkout would lose."""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()
    await _fill(store)
    await store.init()
    await store.close()

    reopened = SqliteMetaStore(db)
    await reopened.init()
    assert "sqlite_autoindex_archive_map_1" in await _archive_access(reopened)
    await reopened.close()
