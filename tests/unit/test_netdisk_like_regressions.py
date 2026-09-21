"""netdisk LIKE 通配符转义回归测试（与 resources.py 的 like_contains 对齐）。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    st = SqliteMetaStore(tmp_path / "meta.db")
    await st.init()
    yield st
    await st.close()


def _row(path, name):
    return {
        "remote_path": path, "name": name, "is_dir": 0, "size": 1,
        "type": "other", "tags": "",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_search_escapes_underscore_wildcard(store):
    """`_` 是 LIKE 单字符通配符：搜 a_b 不得命中 axb。"""
    await store.upsert_netdisk_rows([
        _row("/g/a_b.bin", "a_b.bin"),
        _row("/g/axb.bin", "axb.bin"),
    ])
    hits = {r["name"] for r in await store.search_netdisk_meta("a_b")}
    assert hits == {"a_b.bin"}


@pytest.mark.asyncio
async def test_search_escapes_percent_wildcard(store):
    """搜 `%` 只能命中名称里真的带 `%` 的行，不得变成匹配全表。"""
    await store.upsert_netdisk_rows([
        _row("/g/plain.bin", "plain.bin"),
        _row("/g/100%.bin", "100%.bin"),
    ])
    hits = {r["name"] for r in await store.search_netdisk_meta("%")}
    assert hits == {"100%.bin"}


@pytest.mark.asyncio
async def test_search_escapes_escape_char(store):
    """`\\` 是 ESCAPE 字符：搜 a\\b 不得被当成转义序列 a+escaped(b)。"""
    await store.upsert_netdisk_rows([
        _row("/g/a\\b.bin", "a\\b.bin"),
        _row("/g/ab.bin", "ab.bin"),
    ])
    hits = {r["name"] for r in await store.search_netdisk_meta("a\\b")}
    assert hits == {"a\\b.bin"}


@pytest.mark.asyncio
async def test_search_matches_tags_with_wildcards(store):
    """tags 列同样走转义后的 pattern。"""
    await store.upsert_netdisk_rows([
        _row("/g/one.bin", "one.bin"),
        _row("/g/two.bin", "two.bin"),
    ])
    await store.set_netdisk_tags("/g/two.bin", "tag_a")
    hits = {r["name"] for r in await store.search_netdisk_meta("tag_a")}
    assert hits == {"two.bin"}


@pytest.mark.asyncio
async def test_search_dir_prefix_is_escaped(store):
    """前缀里的 `_` 也不能当通配符：搜 /g_1/ 不得命中 /gX1/。"""
    await store.upsert_netdisk_rows([
        _row("/g_1/a.bin", "a.bin"),
        _row("/gX1/a.bin", "a.bin"),
    ])
    hits = {
        r["remote_path"]
        for r in await store.search_netdisk_meta("a.bin", "/g_1/")
    }
    assert hits == {"/g_1/a.bin"}


@pytest.mark.asyncio
async def test_get_netdisk_meta_prefix_is_escaped(store):
    """get_netdisk_meta 的前缀同样转义。"""
    await store.upsert_netdisk_rows([
        _row("/g_1/a.bin", "a.bin"),
        _row("/gX1/a.bin", "a.bin"),
    ])
    hits = {r["remote_path"] for r in await store.get_netdisk_meta("/g_1/")}
    assert hits == {"/g_1/a.bin"}
