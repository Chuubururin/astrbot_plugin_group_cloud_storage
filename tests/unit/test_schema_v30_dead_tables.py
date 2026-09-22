"""v30 僵尸表清退回归（P3b）。

v17-25 曾预建 scan_claims / fts_dirty_queue / fts_state / outbox_events，
但对应代码路径从未落地（claim_scan 零调用方、异步 FTS worker 从未实现、
SSE 推送是纯内存），v30 整组 DROP。本文件钉死三点：
1) 全新库 init 后不含这些表；
2) 带僵尸表的存量库升级后被清空且版本推进；
3) 后续迁移不得重建（防复活），租约方法已随表移除。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from adapters.persistence.sqlite import SqliteMetaStore, migrations as _m

DEAD_TABLES = ("scan_claims", "fts_dirty_queue", "fts_state", "outbox_events")


def _tables(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fresh_store_has_no_dead_tables(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    try:
        got = _tables(tmp_path / "meta.db")
    finally:
        await s.close()
    assert not got & set(DEAD_TABLES)
    # TTL 调度本体仍在使用（core/runtime/lifecycle 依赖），不得顺手清掉
    assert "scan_schedule" in got


@pytest.mark.asyncio
async def test_upgrade_from_v29_clears_dead_tables(tmp_path):
    """按 v1..v(SCHEMA_VERSION-1) 手工建一个含僵尸表的存量库。"""
    db = tmp_path / "meta.db"
    conn = sqlite3.connect(db)
    for v in sorted(_m.MIGRATIONS):
        if v >= _m.SCHEMA_VERSION:
            continue
        for sql in _m.MIGRATIONS[v]:
            conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (v,)
        )
    conn.commit()
    conn.close()
    assert set(DEAD_TABLES) <= _tables(db)  # 旧链确实预建过它们

    s = SqliteMetaStore(db)
    await s.init()
    await s.close()

    assert not _tables(db) & set(DEAD_TABLES)
    conn = sqlite3.connect(db)
    try:
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()
    assert ver == _m.SCHEMA_VERSION


def test_no_later_migration_recreates_dead_tables():
    """防复活：29 之后的任何迁移不得再 CREATE 这四张表（带不带
    IF NOT EXISTS 都算）。"""
    for v, stmts in _m.MIGRATIONS.items():
        if v <= 29:
            continue
        joined = " ".join(stmts).lower()
        for t in DEAD_TABLES:
            assert not re.search(rf"create table (if not exists )?{t}\b", joined), (
                f"v{v} 试图重建僵尸表 {t}"
            )


@pytest.mark.asyncio
async def test_scan_claim_methods_removed(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    try:
        # 表没了，访问面也不能留（StorePart.__getattr__ 按方法名表转发）
        assert not hasattr(s, "claim_scan")
        assert not hasattr(s, "release_scan_claim")
        assert hasattr(s, "upsert_scan_schedule")
        assert hasattr(s, "list_due_scan_groups")
    finally:
        await s.close()
