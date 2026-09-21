"""`query_resources` 的 LIKE 通配符转义回归测试。

背景（U6 审计发现，2026-09-20）
--------------------------------

`adapters/persistence/sqlite/resources.py::query_resources` 内部对三个过滤项
的处理**不一致**：

| 过滤项    | 构造方式                    | 转义 | ESCAPE 声明 |
|-----------|-----------------------------|------|-------------|
| `keyword` | `like_contains(q.keyword)`  | ✅   | ✅          |
| `exts`    | `f"%{el}"` / `f"%{el}.%"`   | ❌   | ❌          |
| `tags`    | `f'%"{tag}"%'`              | ❌   | ❌          |

`tags` 的实参来自 `webapi/resources.py:81`

    q_tags = [t[1:] for t in tokens if t.startswith("#") and len(t) > 1]

即**用户在搜索框里手打的 `#标签` 词**，只校验了非空。于是 `#a_b` 中的 `_`
被 SQLite 当作"任意单字符"通配符，把 `aXb`、`a%b` 等不相关行一并匹配出来。

对照：netdisk 路径（`adapters/persistence/sqlite/netdisk.py`）**已有**
`tests/unit/test_netdisk_like_regressions.py` 的 6 条转义用例覆盖 `_` / `%` / `\\`。
resources 路径没有对应覆盖——这正是 U6 审计要堵的"局部修复未铺开"缺口。

标记说明
--------
三个 `xfail` 用例各自**断言正确行为**。缺陷修复后把它们去掉 `xfail` 即为回归保护。
使用 `strict=False` 是刻意的：修复完成后这两组状态（flaky/fix）都应该变绿，
不会因为"意外通过"而把 CI 打红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


def _res(name, source_ref, group="g1"):
    return Resource(
        group_id=group,
        type=ResourceType.FILE,
        name=name,
        source_ref=source_ref,
        size=100,
        uploader_id="10001",
        uploader_name="Alice",
        busid=102,
        folder_id="dir1",
        folder_name="Docs",
        created_at=1700000001,
        status=ResourceStatus.ACTIVE,
        meta={},
    )


async def _seed(store, specs):
    """specs: list of (name, source_ref, tags|None)"""
    await store.upsert_resources([_res(n, ref) for n, ref, _ in specs])
    for _name, ref, tags in specs:
        if tags:
            row = await store.get_resource_by_resource_id(f"g1:file:{ref}")
            await store.update_resource_tags(int(row["id"]), tags)


async def _names(store, **kw):
    page = await store.query_resources(
        ResourceQuery(group_id="g1", type="file", page=1, page_size=100, **kw)
    )
    return {i.name for i in page.items}


# --------------------------------------------------------------------------
# 正向对照：keyword 路径**已经**正确，用于锚定修复模板
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_filter_already_escapes_underscore(store):
    """`keyword` 已走 `like_contains`，是同一函数里**正确**的那个。

    作用：把"正确的样子"钉在测试里，作为 tags/exts 的修复模板。
    这条**必须通过**——若它变红，说明 keyword 的转义被回退了。
    """
    await _seed(store, [("a_b.bin", "r1", None), ("aXb.bin", "r2", None)])

    assert await _names(store, keyword="a_b") == {"a_b.bin"}, (
        "keyword 路径本应已转义；失败说明转义被回退"
    )


# --------------------------------------------------------------------------
# tags 过滤：`_` 必须是字面量（U6.5 已修复）
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tag_filter_does_not_treat_underscore_as_wildcard(store):
    """`#a_b` 只能命中真的带 `a_b` 标签的行，不得命中 `aXb`。

    对照 netdisk：`test_search_escapes_underscore_wildcard`。
    """
    await _seed(
        store,
        [("exact.bin", "r1", ["a_b"]), ("other.bin", "r2", ["aXb"])],
    )

    hits = await _names(store, tags=["a_b"])
    assert hits == {"exact.bin"}, f"`_` 渗漏成通配符：期望 {{'exact.bin'}}，实际 {sorted(hits)}"


# --------------------------------------------------------------------------
# tags 过滤：`%` 不得放大成全表匹配
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tag_filter_does_not_treat_percent_as_wildcard(store):
    """`%` 必须按字面量匹配，不得递归放大成全表匹配。

    实测证据（U6.5）：修复前 `tags=["%"]` 命中全部 4 行（`"%` 作为通配符吞掉了所有标签）；
    修复后命中 0 行——因为没有任何一个标签就叫单独一个 `%`。

    不断言具体行数（那会把“无匹配”与“错误匹配”混为一谈），只锁定
    “不得递归命中未携带 `%` 的行”这一不变量。
    """
    await _seed(
        store,
        [("pct.bin", "r1", ["100%"]), ("plain.bin", "r2", ["plain"])],
    )

    hits = await _names(store, tags=["%"])
    assert "plain.bin" not in hits, (
        f"`%` 渗漏成通配符：无关行 plain.bin 被命中（{hits}）"
    )
    # 真实场景下用户搜的是完整标签，这条必须稳定命中
    assert await _names(store, tags=["100%"]) == {"pct.bin"}



# --------------------------------------------------------------------------
# exts 过滤：`_` 必须是字面量
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ext_filter_does_not_treat_underscore_as_wildcard(store):
    """扩展名里的 `_` 必须按字面量匹配，不得退化成"任意单字符"。"""
    await _seed(store, [("a.z_p", "r1", None), ("b.zXp", "r2", None)])

    hits = await _names(store, exts=["z_p"])
    assert hits == {"a.z_p"}, f"exts 的 `_` 渗漏成通配符：期望 {{'a.z_p'}}，实际 {sorted(hits)}"
