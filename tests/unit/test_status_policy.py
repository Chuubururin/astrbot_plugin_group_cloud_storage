"""status_policy — the transition tables and their generated SQL must agree.

Two views of one rule exist because ``ON CONFLICT ... DO UPDATE`` cannot call
Python per row: :func:`merge_*_status` for code, and :func:`*_status_sql` for
the statement. These tests pin them to each other and to the *original*
hand-written CASE semantics, so a table edit that silently changes behaviour
fails here rather than in production.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.persistence.sqlite import status_policy as sp  # noqa: E402


# ------------------------------------------------- resources: Python view


def _original_resource_case(existing: str, incoming: str) -> str:
    """The hand-written CASE v29 replaced, kept as the reference semantics."""
    if incoming != "active":
        return incoming
    if existing == "deleted":
        return incoming
    return existing


@pytest.mark.parametrize("existing", ["active", "deleted", "archived"])
@pytest.mark.parametrize("incoming", ["active", "deleted", "archived"])
def test_resource_python_view_matches_original_case(existing, incoming):
    assert sp.merge_resource_status(existing, incoming) == _original_resource_case(
        existing, incoming
    )


def test_resource_new_row_takes_incoming_status():
    assert sp.merge_resource_status(None, "active") == "active"
    assert sp.merge_resource_status(None, "deleted") == "deleted"


def test_active_does_not_clobber_archived():
    """The user decision that motivated the table's third invariant."""
    assert sp.merge_resource_status("archived", "active") == "archived"


def test_active_revives_deleted():
    assert sp.merge_resource_status("deleted", "active") == "active"


# --------------------------------------------------- volumes: Python view


def _original_volume_case(existing: str, incoming: str) -> str:
    return existing if existing == "uploaded" else incoming


@pytest.mark.parametrize("existing", ["pending", "uploading", "uploaded", "deleted"])
@pytest.mark.parametrize("incoming", ["pending", "uploading", "uploaded"])
def test_volume_python_view_matches_original_case(existing, incoming):
    assert sp.merge_volume_status(existing, incoming) == _original_volume_case(
        existing, incoming
    )


def test_uploaded_part_is_terminal():
    assert sp.merge_volume_status("uploaded", "pending") == "uploaded"


def test_crashed_uploading_part_is_restartable():
    assert sp.merge_volume_status("uploading", "pending") == "pending"


def test_unlisted_status_raises_rather_than_guessing():
    """An unlisted pair is a modelling gap; silently absorbing it hides bugs."""
    with pytest.raises(KeyError):
        sp._resolve({("a", "b"): "c"}, "x", "y")


# ------------------------------------------------------- SQL projections


def _run_resource_case(conn, existing: str, incoming: str) -> str:
    conn.execute("DELETE FROM t")
    conn.execute(
        "INSERT INTO t (id, status) VALUES (1, ?)", (existing,)
    )
    conn.execute(
        f"INSERT INTO t (id, status) VALUES (1, ?) "
        f"ON CONFLICT(id) DO UPDATE SET status={sp.resource_status_sql()}",
        (incoming,),
    )
    return conn.execute("SELECT status FROM t WHERE id=1").fetchone()[0]


@pytest.mark.parametrize("existing", ["active", "deleted", "archived"])
@pytest.mark.parametrize("incoming", ["active", "deleted", "archived"])
def test_resource_sql_matches_python_view(existing, incoming):
    """The generated SQL is executed by SQLite and compared to the table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)")
    got = _run_resource_case(conn, existing, incoming)
    assert got == sp.merge_resource_status(existing, incoming)


def _run_volume_case(conn, existing: str, incoming: str) -> str:
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t (id, status) VALUES (1, ?)", (existing,))
    conn.execute(
        f"INSERT INTO t (id, status) VALUES (1, ?) "
        f"ON CONFLICT(id) DO UPDATE SET status={sp.volume_status_sql()}",
        (incoming,),
    )
    return conn.execute("SELECT status FROM t WHERE id=1").fetchone()[0]


@pytest.mark.parametrize("existing", ["pending", "uploading", "uploaded", "deleted"])
@pytest.mark.parametrize("incoming", ["pending", "uploading", "uploaded"])
def test_volume_sql_matches_python_view(existing, incoming):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)")
    got = _run_volume_case(conn, existing, incoming)
    assert got == sp.merge_volume_status(existing, incoming)


def test_generated_sql_drops_wildcard_rows():
    """Wildcard entries describe fall-through, not a WHEN clause."""
    sql = sp.resource_status_sql()
    assert "'*'" not in sql, "a wildcard leaked into the generated SQL"


def test_generated_sql_has_an_else_branch():
    assert sp.resource_status_sql().rstrip().endswith("ELSE excluded.status END")
    assert sp.volume_status_sql().rstrip().endswith("ELSE excluded.status END")
