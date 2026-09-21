"""迁移原子性与 store 可用性回归（M4/M5/M6/M18/M21）。

背景：
- M4：v13 的 DROP archive_map 与 RENAME 曾是两条独立提交，中断后库里只剩
  archive_map_new，重跑 v13 报 "no such table: archive_map"。
- M5：init() 的 BEGIN 被 migrate() 里的 executescript() 隐式提交，迁移链
  并非原子，异常分支的 rollback() 是空操作。
- M6：reset_and_rebuild 失败后 store 永久不可用（_closed=True 且未恢复）。
- M18/M21：全库 PRAGMA / 重建 IO 在事件循环里同步执行。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite import integrity as _integrity  # noqa: E402
from adapters.persistence.sqlite import store as _store_mod  # noqa: E402
# 通过模块属性实时取值：test_architecture 会 importlib.reload(migrations)，
# 此时 import 时绑定的别名会指向被替换掉的旧对象，patch 将静默失效。
from adapters.persistence.sqlite import migrations as _migrations  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402


class _LoopTicker:
    """统计事件循环在某个 await 期间转了多少圈（用来证明 loop 没被卡住）。"""

    def __init__(self) -> None:
        self.ticks = 0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_LoopTicker":
        async def _run() -> None:
            while True:
                await asyncio.sleep(0.01)
                self.ticks += 1

        self._task = asyncio.create_task(_run())
        return self

    async def __aexit__(self, *_exc) -> bool:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        return False


def _res(i: int, group: str = "g1") -> Resource:
    return Resource(
        group_id=group, type=ResourceType.FILE, name=f"f{i}.zip", source_ref=f"ref_{i}",
        size=i * 100, uploader_id="10001", uploader_name="Alice", busid=102,
        folder_id="dir1", folder_name="Docs", created_at=1700000000 + i,
    )


def _build_v12_db(path: Path, archive_rows: list[tuple] | None = None) -> None:
    """按迁移契约真实构造 v12 库（逐段执行迁移 1..12 + 版本 12）。"""
    conn = sqlite3.connect(path)
    for v in range(1, 13):
        for sql in _migrations.MIGRATIONS[v]:
            conn.executescript(sql)
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (v,))
    for row in archive_rows or []:
        conn.execute(
            "INSERT INTO archive_map (resource_id, group_id, task_id, remote_path, "
            "direction, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


def _schema_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()


# ---------- splitter ----------


def _declared_statement_count(script: str) -> int:
    """从迁移定义推导语句条数：顶层分号切出的非空片段数。

    这里的两段脚本没有触发器体、也没有含分号的字符串字面量，所以按 ';' 切分
    就是迁移定义里声明的语句数。旧断言把 5 / 2 写死，迁移一改（例如给
    MIGRATIONS[1] 再加一条索引）就误报，而切分逻辑本身没坏。
    """
    return len([chunk for chunk in script.split(";") if chunk.strip()])


def test_split_statements_keeps_trigger_bodies_and_splits_multi_statement_scripts():
    """切分必须不拆断触发器体，也不能把一行里的多条语句当成一条。"""
    v1 = _migrations.MIGRATIONS[1][0]
    got = _migrations.split_statements(v1)
    # 期望值从迁移定义推导（CREATE TABLE + N 个 CREATE INDEX），不写死数字
    assert len(got) == _declared_statement_count(v1)
    # 不变式：每一段都是完整语句，不留半句；建表语句恰好一条
    assert all(sqlite3.complete_statement(stmt + ";") for stmt in got)
    assert sum(1 for stmt in got if stmt.upper().startswith("CREATE TABLE")) == 1

    v21 = _migrations.MIGRATIONS[21][0]
    got21 = _migrations.split_statements(v21)
    # 同一行里的 CREATE + INSERT 必须被拆开
    assert len(got21) == _declared_statement_count(v21) == 2
    assert got21[0].upper().startswith("CREATE TABLE")
    assert got21[1].upper().startswith("INSERT")

    # 按内容定位触发器，不写死下标：v17 里插一条语句就会让 [17][6] 静默测到
    # 别的 SQL 上，而断言照样绿。
    trigger = next(
        s for s in _migrations.MIGRATIONS[17]
        if s.lstrip().upper().startswith("CREATE TRIGGER")
        and "AFTER INSERT" in s.upper()
    )
    assert _migrations.split_statements(trigger) == [trigger.strip()]
    # 触发器体内的分号保留（未被误切）
    assert _migrations.split_statements(trigger)[0].rstrip(";").count(";") >= 1


# ---------- M4 / M5 ----------


@pytest.mark.asyncio
async def test_failed_v13_step_leaves_no_half_migration(tmp_path, monkeypatch):
    """M4/M5：v13 中途失败时必须整体回滚。

    旧行为：executescript() 隐式提交，DROP 已落库而 RENAME 未执行，库里只剩
    archive_map_new；且错误被 except sqlite3.Error 吞掉，版本号停在 12。
    """
    db = tmp_path / "meta.db"
    _build_v12_db(db, [(1, "g1", "t1", "/remote/a", "up", "pending", "2026-01-01")])

    monkeypatch.setitem(
        _migrations.MIGRATIONS, 13, list(_migrations.MIGRATIONS[13]) + ["INSERT INTO no_such_table VALUES (1);"]
    )

    store = SqliteMetaStore(db)
    with pytest.raises(sqlite3.Error):
        await store.init()
    await store.close()

    names = _schema_names(db)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT resource_id, remote_path FROM archive_map").fetchall()
    conn.close()

    assert _version(db) == 12            # 版本号未推进
    assert "archive_map" in names        # DROP 与 RENAME 落在同一事务，一起回滚
    assert "archive_map_new" not in names  # 没有半迁移残留
    assert rows == [(1, "/remote/a")]    # 数据未丢


@pytest.mark.asyncio
async def test_migration_chain_is_atomic_on_a_fresh_db(tmp_path, monkeypatch):
    """M5：迁移链现在是一个事务——失败后连 v1..v19 也不该落库。"""
    monkeypatch.setitem(
        _migrations.MIGRATIONS, 20,
        ["CREATE TABLE IF NOT EXISTS marker(a);", "INSERT INTO no_such_table VALUES (1);"],
    )
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    with pytest.raises(sqlite3.Error):
        await store.init()
    await store.close()

    conn = sqlite3.connect(db)
    objects = conn.execute("SELECT name FROM sqlite_master").fetchall()
    conn.close()
    assert objects == []  # executescript 的隐式提交已不再把前面的步骤留下


@pytest.mark.asyncio
async def test_v13_recovers_db_left_with_only_archive_map_new(tmp_path):
    """M4 修复：已被旧缺陷弄坏的库（只剩 archive_map_new）能自行修复并升级。"""
    db = tmp_path / "meta.db"
    _build_v12_db(db, [
        (1, "g1", "t1", "/remote/a", "up", "pending", "2026-01-01"),
        (2, "g1", "t2", "/remote/b", "down", "done", "2026-01-02"),
    ])

    # 复现旧缺陷：copy 与 DROP 已各自提交，RENAME 前中断
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE archive_map_new (
            resource_id  INTEGER NOT NULL,
            group_id     TEXT    NOT NULL,
            task_id      TEXT,
            remote_path  TEXT    NOT NULL,
            direction    TEXT    NOT NULL,
            state        TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (resource_id, group_id, direction)
        );
        INSERT INTO archive_map_new SELECT * FROM archive_map;
        DROP TABLE archive_map;
    """)
    conn.commit()
    conn.close()
    assert "archive_map" not in _schema_names(db)  # 前提：只剩 archive_map_new

    store = SqliteMetaStore(db)
    await store.init()  # 不得抛 no such table: archive_map

    names = _schema_names(db)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT resource_id, remote_path FROM archive_map ORDER BY resource_id"
    ).fetchall()
    conn.close()
    await store.close()

    assert _version(db) == _migrations.SCHEMA_VERSION   # 版本号终于能越过 12
    assert "archive_map" in names and "archive_map_new" not in names
    assert rows == [(1, "/remote/a"), (2, "/remote/b")]  # 已搬过去的数据保留


# ---------- M6 ----------


@pytest.mark.asyncio
async def test_store_stays_usable_when_rebuild_fails(tmp_path, monkeypatch):
    """M6：rebuild 失败后 store 必须仍可用（不能永久 _closed）。"""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()
    await store.upsert_resources([_res(1)])

    def _boom(conn):
        raise RuntimeError("migration exploded")

    with monkeypatch.context() as m:
        m.setattr(_store_mod, "migrate", _boom)
        with pytest.raises(RuntimeError):
            await store.reset_and_rebuild()

    # 失败后 store 仍可读写（旧行为会抛 "connection manager is closed"）
    await store.upsert_resources([_res(2)])
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert page.total == 2  # 原库未被半成品替换

    # 临时文件已清理，且后续正常 rebuild 依然可用
    assert list(tmp_path.glob("*.rebuild")) == []
    await store.reset_and_rebuild()
    page = await store.query_resources(ResourceQuery(group_id="g1", page_size=10))
    assert page.total == 0
    assert _version(db) == _migrations.SCHEMA_VERSION
    await store.close()


# ---------- M21 ----------


@pytest.mark.asyncio
async def test_reset_and_rebuild_runs_off_the_event_loop(tmp_path, monkeypatch):
    """M21：重建（sqlite3 / PRAGMA / os.replace）必须 offload 到线程。"""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()

    loop_tid = threading.get_ident()
    seen: dict[str, int] = {}
    real = _store_mod._rebuild_database

    def _slow(db_path):
        seen["tid"] = threading.get_ident()
        time.sleep(0.3)
        return real(db_path)

    monkeypatch.setattr(_store_mod, "_rebuild_database", _slow)

    async with _LoopTicker() as ticker:
        await store.reset_and_rebuild()

    assert seen["tid"] != loop_tid  # 阻塞部分跑在工作线程
    assert ticker.ticks >= 5        # 期间事件循环照常运行
    assert _version(db) == _migrations.SCHEMA_VERSION
    await store.close()


# ---------- M18 ----------


@pytest.mark.asyncio
async def test_check_integrity_runs_off_the_event_loop(tmp_path, monkeypatch):
    """M18：PRAGMA integrity_check（全库扫描）必须 offload 到线程。"""
    db = tmp_path / "meta.db"
    store = SqliteMetaStore(db)
    await store.init()
    await store.close()

    loop_tid = threading.get_ident()
    seen: dict[str, int] = {}
    real = _integrity._check_integrity_sync

    def _slow(path):
        seen["tid"] = threading.get_ident()
        time.sleep(0.3)
        return real(path)

    monkeypatch.setattr(_integrity, "_check_integrity_sync", _slow)

    async with _LoopTicker() as ticker:
        result = await _integrity.check_integrity(db)

    assert result["ok"] is True
    assert seen["tid"] != loop_tid
    assert ticker.ticks >= 5


@pytest.mark.asyncio
async def test_integrity_check_reports_missing_database(tmp_path):
    """回归：offload 后返回值语义不变（缺库仍返回 ok=False）。"""
    result = await _integrity.check_integrity(tmp_path / "nope.db")
    assert result["ok"] is False
    assert result["errors"] == ["database file not found"]
