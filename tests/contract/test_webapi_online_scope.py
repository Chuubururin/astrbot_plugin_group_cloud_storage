"""WebAPI handler 测试：files/stat 跨群聚合的在线缺省口径。

需求简报缺省规则（2026-09-07）：未指定群/账号时，files 与 stat 的聚合范围
= 全部在线账号所属群（凋零语义：离线账号的群不进入缺省聚合，数据不删除）；
`account` 参数指定单账号时不再叠加在线过滤（显式指定即用户意图）。

Run: pytest tests/contract/test_webapi_online_scope.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapi import webapi_base as _wb  # noqa: E402
from webapi.resources import api_files, api_stat  # noqa: E402
from core.domain.sync import GroupInfo, Page  # noqa: E402


def _json_response(data):
    return data


def _error_response(msg, status_code=400):
    return {"status": "error", "message": msg, "status_code": status_code}


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr("webapi.resources.json_response", _json_response)
    monkeypatch.setattr("webapi.resources.error_response", _error_response)
    # _group_open_error resolves error_response in webapi_base; patch it too
    # so gate failures never reach the real astrbot handler.
    monkeypatch.setattr("webapi.webapi_base.error_response", _error_response)
    # Managed-group list cache is module-global: reset between tests.
    monkeypatch.setattr(_wb, "_GROUP_LIST_CACHE", {"key": None, "at": 0.0, "groups": None})


class _FakeRequestQuery:
    """request.query.get(key, default, type=...) minimal stub."""

    def __init__(self, params: dict):
        self._params = params

    def get(self, key, default=None, type=None):
        v = self._params.get(key)
        if v is None:
            return default
        if type is not None and not isinstance(v, type):
            try:
                return type(v)
            except (TypeError, ValueError):
                return default
        return v

    async def json(self, default=None):
        return {}


def _patch_request(monkeypatch, params: dict):
    fake = SimpleNamespace(query=_FakeRequestQuery(params), json=None)
    async def _json(default=None):
        return {}
    fake.json = _json
    # _param reads the request bound in webapi_base; handlers read resources'.
    monkeypatch.setattr("webapi.resources.request", fake)
    monkeypatch.setattr("webapi.webapi_base.request", fake)


def _make_services(groups: list[GroupInfo], online_ids: set[str]) -> SimpleNamespace:
    """Services stub covering only the paths api_files/api_stat touch."""

    class _Scan:
        async def list_page_groups(self, managed_groups):
            return list(groups)

        async def is_page_managed(self, group_id, managed_groups):
            return True

        async def assert_group_openable(self, group_id, managed_groups):
            pass  # stub: all groups are openable in tests

    class _Query:
        def __init__(self):
            self.seen: list = []

        async def page_with(self, rq):
            self.seen.append(rq)
            return Page(items=[], total=0, page=rq.page, page_size=rq.page_size)

    class _Store:
        async def list_folders_detail(self, group):
            return []

        async def list_archived_done_ids(self, ids, direction=""):
            return set()

        async def tag_cloud(self, kind=None):
            return []

        async def sum_resource_sizes(self, group_id):
            return 0

        async def list_groups(self, include_hidden: bool = False):
            return list(groups)

    return SimpleNamespace(
        config={"managed_groups": [], "page_size": 10, "type_ext_overrides": {}},
        scan=_Scan(),
        query=_Query(),
        store=_Store(),
        stats=None,  # only touched when a concrete group is given
        searchkv=None,
        get_online_account_ids=lambda: set(online_ids),
    )


def _groups() -> list[GroupInfo]:
    return [
        GroupInfo(group_id="g1", account_id="acc1"),
        GroupInfo(group_id="g2", account_id="acc2"),
        GroupInfo(group_id="g3", account_id="acc1"),
    ]


@pytest.mark.asyncio
async def test_files_default_scope_online_accounts_only(monkeypatch):
    """未指定群/账号 → 仅聚合在线账号所属群（离线账号 acc2 的 g2 排除）。"""
    _patch_request(monkeypatch, {})
    svc = _make_services(_groups(), online_ids={"acc1"})
    await api_files(svc)
    rq = svc.query.seen[0]
    assert sorted(rq.groups) == ["g1", "g3"]


@pytest.mark.asyncio
async def test_files_account_filter_single_account(monkeypatch):
    """指定 account → 仅该账号的群（不再叠加在线过滤）。"""
    _patch_request(monkeypatch, {"account": "acc2"})
    svc = _make_services(_groups(), online_ids={"acc1"})
    await api_files(svc)
    rq = svc.query.seen[0]
    assert rq.groups == ["g2"]


@pytest.mark.asyncio
async def test_files_explicit_group_keeps_single_scope(monkeypatch):
    """指定群 → 单群查询（groups 聚合条件不启用）。"""
    _patch_request(monkeypatch, {"group": "g2"})
    svc = _make_services(_groups(), online_ids=set())
    await api_files(svc)
    rq = svc.query.seen[0]
    assert rq.group_id == "g2"
    assert rq.groups is None


@pytest.mark.asyncio
async def test_files_unknown_online_set_keeps_owned_groups(monkeypatch):
    """在线集合未知（回调未接线）→ 仅保留无账号归属的群（不误隐藏 owned 群）。"""
    _patch_request(monkeypatch, {})
    svc = _make_services(_groups(), online_ids=set())
    await api_files(svc)
    rq = svc.query.seen[0]
    assert rq.groups == []


@pytest.mark.asyncio
async def test_stat_default_scope_online_accounts_only(monkeypatch):
    """stat 聚合同口径：离线账号群的容量/容量缺省不进入聚合。"""
    _patch_request(monkeypatch, {})
    groups = _groups()
    for g in groups:
        g.used_space = 100
        g.total_space = 10 * 1024 ** 3
    svc = _make_services(groups, online_ids={"acc1"})
    result = await api_stat(svc)
    assert result["group_id"] == "*"
    assert result["used_space"] == 200  # g1 + g3，排除 g2
    assert result["total_space"] == 20 * 1024 ** 3
    # 聚合查询同样只含在线账号的群
    rq = svc.query.seen[0]
    assert sorted(rq.groups) == ["g1", "g3"]
    # 操作者 = 统计范围内的账号去重集合（缺省口径=在线账号）
    assert result["accounts"] == ["acc1"]


@pytest.mark.asyncio
async def test_stat_account_filter_single_account(monkeypatch):
    """stat 指定 account → 仅该账号。"""
    _patch_request(monkeypatch, {"account": "acc2"})
    groups = _groups()
    for g in groups:
        g.used_space = 100
        g.total_space = 10 * 1024 ** 3
    svc = _make_services(groups, online_ids={"acc1"})
    result = await api_stat(svc)
    assert result["used_space"] == 100
    rq = svc.query.seen[0]
    assert rq.groups == ["g2"]
    assert result["accounts"] == ["acc2"]


@pytest.mark.asyncio
async def test_stat_single_group_reports_owning_account(monkeypatch):
    """单群统计的操作者 = 群归属账号（群文件操作由该账号的 bot 执行）。"""
    _patch_request(monkeypatch, {"group": "g1"})
    groups = _groups()
    svc = _make_services(groups, online_ids={"acc1"})

    class _Stats:
        from core.domain.sync import ResourceStats as _RS

        async def stats(self, group_id):
            return self._RS(
                group_id=group_id, file_count=1, total_size=1,
                uploaders=1, used_space=1, total_space=1,
            )

    svc.stats = _Stats()
    result = await api_stat(svc)
    assert result["group_id"] == "g1"
    assert result["accounts"] == ["acc1"]


@pytest.mark.asyncio
async def test_stat_single_group_unknown_account_empty(monkeypatch):
    """群归属账号未记录（历史数据）→ accounts 为空列表（前端显示 '-'）。"""
    _patch_request(monkeypatch, {"group": "g9"})
    groups = _groups()  # 无 g9
    svc = _make_services(groups, online_ids=set())

    class _Stats:
        from core.domain.sync import ResourceStats as _RS

        async def stats(self, group_id):
            return self._RS(
                group_id=group_id, file_count=1, total_size=1,
                uploaders=1, used_space=1, total_space=1,
            )

    svc.stats = _Stats()
    result = await api_stat(svc)
    assert result["accounts"] == []
