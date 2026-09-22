"""分卷批量查询（P2-8）：`list_volumes_by_parents` 与 files 列表投影的批量口径。

背景：`webapi/resources.py` 的 files 列表投影里，"分卷完整性"原本是**逐行**
`await store.list_volumes(...)`，而紧邻的两处（`list_archived_done_ids`、
`find_cross_store_copies`）都是批量的 —— 同一函数内两种风格并存。

根因（实测，100 行 × 5 分卷）：
  逐行串行 await          13.92 ms
  单次线程跳 + IN 查询     1.14 ms   (12.2x)
  单次线程跳 + N 条语句    1.11 ms
⇒ 成本几乎全在**每次 store 调用的线程跳 + 连接池签出**，不在 SQL。

本文件守住两件事：
1. 批量结果与逐行结果**逐键等价**（含 seq 排序、无分卷的 parent 缺席）；
2. files 列表投影**不得**退回逐行查询（spy 计数，可证伪）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.catalog.resource_query import ResourceQueryService  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import GroupInfo, VolumeInfo  # noqa: E402
from webapi import webapi_base as _wb  # noqa: E402
from webapi.resources import api_files  # noqa: E402

GROUP = "g1"
PARENTS = [f"{GROUP}:file:{i}" for i in range(3)]
PARTS = 4
ROWS = 3


def _volumes(parents: list[str]) -> list[VolumeInfo]:
    """每个 parent 4 个分卷，**最后一个故意没有 source_ref** -> 应为 incomplete。"""
    return [
        VolumeInfo(
            parent_resource_id=p,
            seq=s,
            part_name=f"{p}.part{s}",
            source_ref="" if s == PARTS - 1 else f"ref_{p}_{s}",
            busid=s,
            size=100,
            sha256=f"h{p}_{s}",
            status="done",
            upload_time=1,
            group_id=GROUP,
        )
        for p in parents
        for s in range(PARTS)
    ]


class _SpyStore:
    """Delegates to a real store; counts the two volume lookups."""

    def __init__(self, inner: SqliteMetaStore):
        self._inner = inner
        self.per_row = 0
        self.batched = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_volumes(self, parent_resource_id: str):
        self.per_row += 1
        return await self._inner.list_volumes(parent_resource_id)

    async def list_volumes_by_parents(self, parent_resource_ids: list[str]):
        self.batched += 1
        return await self._inner.list_volumes_by_parents(parent_resource_ids)


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


# ---------------- 1. 批量与逐行逐键等价 ----------------

@pytest.mark.asyncio
async def test_batched_lookup_matches_per_row_lookup(store):
    await store.insert_volumes(_volumes(PARENTS))
    batched = await store.list_volumes_by_parents(PARENTS)
    # 反空断言：抽到空则下面的逐键比较会静默空转。
    assert batched, "批量结果为空 —— 本用例会静默空转"
    assert set(batched) == set(PARENTS)
    for p in PARENTS:
        per_row = await store.list_volumes(p)
        assert [v.seq for v in batched[p]] == [v.seq for v in per_row], "seq 排序不一致"
        assert [v.source_ref for v in batched[p]] == [
            v.source_ref for v in per_row
        ], "字段不一致"


@pytest.mark.asyncio
async def test_batched_lookup_omits_parents_without_volumes(store):
    await store.insert_volumes(_volumes(PARENTS))
    missing = f"{GROUP}:file:999"
    out = await store.list_volumes_by_parents([*PARENTS, missing])
    assert set(out) == set(PARENTS), "无分卷的 parent 不应出现在结果里"
    assert await store.list_volumes(missing) == []


@pytest.mark.asyncio
async def test_batched_lookup_empty_input(store):
    await store.insert_volumes(_volumes(PARENTS))
    assert await store.list_volumes_by_parents([]) == {}
    # 反空断言：非空输入必须真返回行，否则上面的 {} 断言毫无意义。
    assert await store.list_volumes_by_parents(PARENTS[:1]), "非空输入返回空 —— 探针失效"


# ---------------- 2. files 列表投影不得退回逐行 ----------------

class _FakeQuery:
    def __init__(self, params: dict):
        self._p = params

    def get(self, key, default=None, type=None):
        v = self._p.get(key)
        if v is None:
            return default
        if type is not None and not isinstance(v, type):
            try:
                return type(v)
            except (TypeError, ValueError):
                return default
        return v


@pytest.fixture(autouse=True)
def _patch_webapi(monkeypatch):
    monkeypatch.setattr("webapi.resources.json_response", lambda data: data)
    for target in ("webapi.resources.error_response", "webapi.webapi_base.error_response"):
        monkeypatch.setattr(target, lambda msg, status_code=400: {"status": "error", "message": msg})
    monkeypatch.setattr(_wb, "_GROUP_LIST_CACHE", {"key": None, "at": 0.0, "groups": None})


def _patch_request(monkeypatch, params: dict):
    async def _json(default=None):
        return {}

    fake = SimpleNamespace(query=_FakeQuery(params), json=_json)
    monkeypatch.setattr("webapi.resources.request", fake)
    monkeypatch.setattr("webapi.webapi_base.request", fake)


def _services(store, spy, groups) -> SimpleNamespace:
    class _Scan:
        async def list_page_groups(self, managed_groups):
            return list(groups)

        async def is_page_managed(self, group_id, managed_groups):
            return True

        async def assert_group_openable(self, group_id, managed_groups):
            pass  # stub: all groups openable in tests

    return SimpleNamespace(
        config={"managed_groups": [], "page_size": 50, "type_ext_overrides": {}},
        scan=_Scan(),
        query=ResourceQueryService(store),
        store=spy,
        stats=None,
        searchkv=None,
        ready=None,
        get_online_account_ids=lambda: {g.account_id for g in groups},
    )


@pytest.mark.asyncio
async def test_files_list_batches_volume_lookup(store, monkeypatch):
    """列表投影对整页只做一次分卷查询（旧实现是每行一次）。"""
    await store.upsert_resources([
        Resource(
            group_id=GROUP,
            type=ResourceType.FILE,
            name=f"big{i}.zip",
            source_ref=f"ref_{i}",
            size=1000,
            created_at=1700000000 + i,
            meta={"volumes": [{"seq": s} for s in range(PARTS)]},
        )
        for i in range(ROWS)
    ])
    # resource_id 形如 "g1:file:<id>"，从查询结果取，避免手写格式漂移。
    page = await ResourceQueryService(store).page(GROUP, page=1, page_size=50)
    parents = [it.resource_id for it in page.items]
    assert len(parents) == ROWS, f"期望 {ROWS} 行带分卷的资源，实际 {len(parents)}"
    await store.insert_volumes(_volumes(parents))

    spy = _SpyStore(store)
    _patch_request(monkeypatch, {})
    out = await api_files(_services(store, spy, [GroupInfo(group_id=GROUP, account_id="acc1")]))

    # 反空断言：这一页必须真的带上了分卷状态，否则下面的计数断言是空的。
    with_vol = [it for it in out["items"] if it.get("is_volume")]
    assert len(with_vol) == ROWS, f"这一页没有 {ROWS} 行带分卷的资源（实际 {len(with_vol)}）"
    assert all(it["volume_total"] == PARTS for it in with_vol)
    assert all(it["volume_complete"] is False for it in with_vol), "最后一个分卷无 source_ref"

    assert spy.per_row == 0, f"列表投影又退回逐行 list_volumes（{spy.per_row} 次）"
    assert spy.batched == 1, f"整页应只做 1 次批量分卷查询，实际 {spy.batched} 次"
