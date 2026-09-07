"""任务记录/操作流/群组隐藏 存储层测试（v15，ADR-0005 经纠偏 D-6 实施）。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite.migrations import (  # noqa: E402
    MIGRATIONS as _MIGRATIONS,
    SCHEMA_VERSION as _SCHEMA_VERSION,
)
from core.domain.resource import Resource  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


def _res(i: int, group: str = "g1") -> Resource:
    return Resource(
        group_id=group, type=ResourceType.FILE, name=f"f{i}.zip", source_ref=f"ref_{i}",
        size=i * 100, uploader_id="10001", uploader_name="Alice", busid=102,
        folder_id="dir1", folder_name="Docs", created_at=1700000000 + i,
    )


# ---------- v15 迁移双路径（HL-24：全新库 + 旧库升级） ----------

@pytest.mark.asyncio
async def test_v25_fresh_init(tmp_path):
    """路径 A：全新库直接建到 v25，outbox 与 FTS 状态就位。"""
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    # 直接检查底层结构（经 store 的连接不便直接访问，用独立连接只读）
    conn = sqlite3.connect(tmp_path / "meta.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "op_ledger" in tables and "op_ops" in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(groups)").fetchall()}
    assert "hidden" in cols
    ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert ver == _SCHEMA_VERSION  # 版本号以 migrations.SCHEMA_VERSION 为准


@pytest.mark.asyncio
async def test_v25_upgrade_from_v16(tmp_path):
    """路径 B：v16 存量库升级不丢数据，新增 FTS 状态与门控。"""
    db = tmp_path / "meta.db"
    conn = sqlite3.connect(db)
    # 构造 v16 库：按版本化迁移 1..16 逐段执行，写入存量数据，置版本 16
    for v in range(1, 17):
        for sql in _MIGRATIONS[v]:
            conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (v,)
        )
    conn.execute(
        "INSERT INTO groups(group_id, group_name, account_id) VALUES ('g9','旧群','acc1')"
    )
    conn.execute(
        """INSERT INTO resources(resource_id, group_id, type, name, size, source_ref,
           path, ext, status, tags, meta, created_at, indexed_at, updated_at)
           VALUES ('r9','g9','file','旧文件.zip',1,'ref9','/g9/旧文件.zip','',
                   'active','[]','{}',1,1,1)"""
    )
    conn.commit()
    conn.close()

    s = SqliteMetaStore(db)
    await s.init()  # 升级到最新 schema

    conn = sqlite3.connect(db)
    ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert ver == _SCHEMA_VERSION  # 版本号以 migrations.SCHEMA_VERSION 为准
    cols = {r[1] for r in conn.execute("PRAGMA table_info(groups)").fetchall()}
    assert "hidden" in cols
    # 存量群仍在且 hidden 默认 0（不误隐藏）
    row = conn.execute("SELECT group_name, hidden FROM groups WHERE group_id='g9'").fetchone()
    assert row[0] == "旧群" and row[1] == 0
    # 存量资源仍在
    cnt = conn.execute("SELECT COUNT(*) FROM resources WHERE group_id='g9'").fetchone()[0]
    assert cnt == 1
    # v16（2026-09-03 容量口径核对）：limit_count 列升级就位
    cols = {r[1] for r in conn.execute("PRAGMA table_info(groups)").fetchall()}
    assert "limit_count" in cols
    conn.close()
    await s.close()





@pytest.mark.asyncio
async def test_ledger_upsert_and_query(store):
    await store.ledger_upsert("t1", "move_file", "g1", {"id": 1}, "pending")
    await store.ledger_upsert("t1", "move_file", "g1", {"id": 1}, "running")
    row = await store.ledger_get("t1")
    assert row["task_id"] == "t1" and row["state"] == "running"
    assert row["payload"] == {"id": 1}

    await store.ledger_upsert("t2", "delete", "g2", None, "done")
    assert len(await store.ledger_query(state="running")) == 1
    assert len(await store.ledger_query(kind="delete")) == 1
    assert len(await store.ledger_query(target="g2")) == 1
    assert len(await store.ledger_query()) == 2

    # 终态清空 error；失败保留 error
    await store.ledger_upsert("t2", "delete", "g2", None, "failed", error="boom")
    assert (await store.ledger_get("t2"))["error"] == "boom"
    await store.ledger_upsert("t2", "delete", "g2", None, "done")
    assert (await store.ledger_get("t2"))["error"] is None


@pytest.mark.asyncio
async def test_ledger_reconcile(store):
    # 白名单（断点续传候选）：转分卷/长视频/网盘索引
    await store.ledger_upsert("t1", "convert_volumes", "g1", None, "running")
    await store.ledger_upsert("t2", "video_upload", "g1", None, "paused")
    # 非白名单：running/paused/pending 一律 failed（重启中断/队列丢失）
    await store.ledger_upsert("t3", "move_file", "g1", None, "running")
    await store.ledger_upsert("t4", "delete", "g1", None, "pending")
    # 终态不受影响
    await store.ledger_upsert("t5", "scan", "g1", None, "done")

    n = await store.ledger_reconcile()
    assert n == 4
    assert (await store.ledger_get("t1"))["state"] == "pending"
    assert (await store.ledger_get("t2"))["state"] == "pending"
    assert (await store.ledger_get("t3"))["state"] == "failed"
    assert (await store.ledger_get("t3"))["error"] == "interrupted by restart"
    assert (await store.ledger_get("t4"))["state"] == "failed"
    assert (await store.ledger_get("t5"))["state"] == "done"


# ---------- 操作流 ----------

@pytest.mark.asyncio
async def test_ops_append_list_and_resource_lookup(store):
    await store.ops_append("t1", "move",
                           before={"group_id": "g1", "id": 7, "folder": ""},
                           after={"group_id": "g1", "id": 7, "folder": "dir2"})
    await store.ops_append("t1", "move",
                           before={"group_id": "g1", "id": 7, "folder": "dir2"},
                           after={"group_id": "g1", "id": 7, "folder": ""})
    ops = await store.ops_list("t1")
    assert len(ops) == 2 and ops[0]["seq"] == 1 and ops[1]["seq"] == 2
    assert ops[0]["before"]["folder"] == "" and ops[1]["before"]["folder"] == "dir2"

    # 直连操作（task_id=''）：按资源定位最近一次
    await store.ops_append("", "tags",
                           before={"group_id": "g1", "id": 9, "tags": ["a"]},
                           after={"group_id": "g1", "id": 9, "tags": ["a", "b"]})
    hit = await store.ops_last_for_resource("tags", 9)
    assert hit is not None and hit["after"]["tags"] == ["a", "b"]
    assert await store.ops_last_for_resource("tags", 999) is None


# ---------- 群组隐藏（账号离线凋零：隐藏非删除） ----------

@pytest.mark.asyncio
async def test_hide_account_groups_and_list_filter(store):
    from core.domain.sync import GroupInfo

    await store.upsert_groups([
        GroupInfo(group_id="g1", group_name="A", account_id="acc1"),
        GroupInfo(group_id="g2", group_name="B", account_id="acc1"),
        GroupInfo(group_id="g3", group_name="C", account_id="acc2"),
    ])
    n = await store.hide_account_groups("acc1", 1)
    assert n == 2
    visible = await store.list_groups()
    assert [g.group_id for g in visible] == ["g3"]
    assert all(g.hidden == 1 for g in await store.list_groups(include_hidden=True)
               if g.group_id in ("g1", "g2"))
    # 账号恢复在线 → 重新显示
    await store.hide_account_groups("acc1", 0)
    assert len(await store.list_groups()) == 3