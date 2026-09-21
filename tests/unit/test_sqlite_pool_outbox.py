"""连接池容量回收与 op_ops 序号原子性回归测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite.connection import ConnectionManager  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


# ---------- 连接池：建连失败必须归还槽位 ----------

@pytest.mark.asyncio
async def test_failed_connect_releases_pool_slot(tmp_path):
    """_connect() 抛异常时 _created 已自增却从不回退：累计 pool_size 次失败后
    池容量被永久吞掉，之后每个调用都要阻塞 30 秒才抛 TimeoutError。"""
    cm = ConnectionManager(tmp_path / "meta.db", pool_size=2)
    real_connect = cm._connect
    failures = {"n": 0}

    def flaky():
        failures["n"] += 1
        if failures["n"] <= 4:  # 2 * pool_size 次失败
            raise RuntimeError("connect failed")
        return real_connect()

    cm._connect = flaky

    with pytest.raises(RuntimeError):
        await cm.execute(lambda conn: None)
    assert cm._created == 0, "失败的建连不得占用池槽位"

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cm.execute(lambda conn: None)
    assert cm._created == 0

    # 池容量仍在：能正常建连并执行
    assert await cm.execute(lambda conn: conn.execute("SELECT 1").fetchone()[0]) == 1
    await cm.close()


# ---------- op_ops 序号 ----------

@pytest.mark.asyncio
async def test_ops_append_seq_strictly_increasing(store):
    for i in range(5):
        await store.ops_append("t1", "move", {"i": i}, {"i": i + 1})
    ops = await store.ops_list("t1")
    assert [o["seq"] for o in ops] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_ops_append_seq_unique_under_concurrency(store):
    """并发写同一 task_id：seq 必须在单条 INSERT ... SELECT 内生成。

    旧实现是两条独立语句（SELECT MAX(seq)+1 再 INSERT），SELECT 在 autocommit
    下读完不持锁，两个写入者可以读到同一个 MAX 而双双插入同一个 seq
    （op_ops 只有普通索引 idx_ops_task，没有唯一约束）。
    """
    await asyncio.gather(
        *(store.ops_append("t1", "move", {"i": i}, {"i": i}) for i in range(24))
    )
    seqs = [o["seq"] for o in await store.ops_list("t1")]
    assert len(seqs) == 24
    assert sorted(seqs) == list(range(1, 25))
