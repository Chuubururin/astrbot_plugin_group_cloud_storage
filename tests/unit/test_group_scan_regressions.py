"""回归测试：群信息扫描。

- M3 相册/精华采集失败时不得用 0 覆盖已存计数
  （upsert_groups 对 album_count/essence_count 是无条件覆盖）
- 低危 scan_owned_incremental 的 include_capacity 之前声明未使用
- L5 incremental：退群/被移除的群必须收敛（managed=0），且不得误伤
  「本次列出了但字段为空」与分片扫描/其它账号的群
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import GroupScanService  # noqa: E402
from core.domain.resource import GroupMember  # noqa: E402
from core.domain.sync import GroupInfo  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402

_COLLECT_PREFIXES = (
    "get_group_file_system_info",
    "get_qun_album_list",
    "get_essence_msg_list",
)


def _collected(api) -> list[str]:
    """本次扫描里真正的云端采集调用（容量/相册/精华）。"""
    return [c for c in api.calls if c.startswith(_COLLECT_PREFIXES)]


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1"],
        bot_qq="10001",
        members_by_group={"g1": [GroupMember("10001", "Me", "owner")]},
    )
    queue = OpQueue(lambda op: None, interval=0.0)
    svc = GroupScanService(api, store, queue)
    yield store, api, svc
    await queue.shutdown()
    await store.close()


async def _group(store, gid="g1"):
    return next(g for g in await store.list_groups() if g.group_id == gid)


# ---------- M3 ----------


@pytest.mark.asyncio
async def test_album_essence_counts_survive_fetch_failure(env, monkeypatch):
    """M3：采集抛异常时 album_c/essence_c 不得停在 0 被写回。"""
    store, api, svc = env
    api.albums = {"g1": [{"album_id": f"a{i}", "name": f"相册{i}"} for i in range(3)]}
    api.essences = {"g1": [{"message_id": "m1"}, {"message_id": "m2"}]}

    await svc.scan_owned()
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (3, 2)

    async def _boom(group_id):
        raise RuntimeError("cloud hiccup")

    monkeypatch.setattr(api, "get_qun_album_list", _boom)
    monkeypatch.setattr(api, "get_essence_msg_list", _boom)
    await svc.scan_owned()
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (3, 2), (
        "采集失败不得把已存计数覆盖为 0"
    )


@pytest.mark.asyncio
async def test_album_count_survives_essence_only_failure(env, monkeypatch):
    """相册采集成功、精华失败：相册用新值，精华回填旧值。"""
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1", "name": "相册"}]}
    api.essences = {"g1": [{"message_id": "m1"}, {"message_id": "m2"}]}
    await svc.scan_owned()
    assert (await _group(store)).essence_count == 2

    api.albums = {
        "g1": [{"album_id": f"a{i}", "name": f"相册{i}"} for i in range(4)]
    }

    async def _boom(group_id):
        raise RuntimeError("essence hiccup")

    monkeypatch.setattr(api, "get_essence_msg_list", _boom)
    await svc.scan_owned()
    g1 = await _group(store)
    assert g1.album_count == 4
    assert g1.essence_count == 2


@pytest.mark.asyncio
async def test_incremental_album_essence_counts_survive_failure(env, monkeypatch):
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1", "name": "相册"}]}
    api.essences = {"g1": [{"message_id": "m1"}, {"message_id": "m2"}]}
    await svc.scan_owned_incremental()
    assert (await _group(store)).album_count == 1

    await store.update_group_fields("g1", total_space=0, last_scan_at=0)

    async def _boom(group_id):
        raise RuntimeError("cloud hiccup")

    monkeypatch.setattr(api, "get_qun_album_list", _boom)
    monkeypatch.setattr(api, "get_essence_msg_list", _boom)
    await svc.scan_owned_incremental()
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (1, 2)


# ---------- 低危：include_capacity ----------


@pytest.mark.asyncio
async def test_incremental_include_capacity_false_skips_collection(env):
    """include_capacity=False 不得全量采集容量与相册/精华。"""
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1", "name": "相册"}]}
    api.essences = {"g1": [{"message_id": "m1"}]}
    await svc.scan_owned_incremental(include_capacity=True)
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (1, 1)
    assert g1.total_space > 0

    # 强制下一次重扫（need=True），同时保留已存容量
    await store.update_group_fields("g1", last_scan_at=0)
    api.calls.clear()
    await svc.scan_owned_incremental(include_capacity=False)
    assert _collected(api) == [], _collected(api)
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (1, 1)
    assert g1.total_space > 0


@pytest.mark.asyncio
async def test_incremental_include_capacity_true_still_collects(env):
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1", "name": "相册"}]}
    api.essences = {"g1": [{"message_id": "m1"}]}
    api.calls.clear()
    await svc.scan_owned_incremental(include_capacity=True)
    collected = _collected(api)
    assert len(collected) == 3, collected
    assert any(c.startswith("get_qun_album_list") for c in collected)
    assert any(c.startswith("get_essence_msg_list") for c in collected)
    g1 = await _group(store)
    assert (g1.album_count, g1.essence_count) == (1, 1)


# ---------- L5 ----------


@pytest.mark.asyncio
async def test_incremental_converges_group_no_longer_listed(env):
    """L5：退群/被移除的群必须收敛（managed=0），不得永久保留陈旧数据。"""
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1", "name": "相册"}]}
    await svc.scan_owned_incremental()
    g1 = await _group(store)
    assert (g1.managed, g1.album_count) == (1, 1)

    # 退群：list_groups() 不再返回 g1
    api.group_ids = []
    await svc.scan_owned_incremental()
    g1 = await _group(store)
    assert g1.managed == 0, "退群后 managed 未收敛"


@pytest.mark.asyncio
async def test_incremental_keeps_listed_group_with_empty_fields(env, monkeypatch):
    """L5：本次列出了但字段为空（群名为空）不得被当成退群收敛。"""
    store, api, svc = env
    await svc.scan_owned_incremental()

    async def _empty_name(no_cache=False):
        return [{"group_id": "g1", "group_name": ""}]

    monkeypatch.setattr(api, "list_groups", _empty_name)
    await svc.scan_owned_incremental()
    g1 = await _group(store)
    assert g1.managed == 1
    assert g1.group_name == ""


@pytest.mark.asyncio
async def test_incremental_sharded_scan_does_not_converge(env):
    """L5：带 group_filter 的分片扫描不是全量真相，不得据此收敛。"""
    store, api, svc = env
    await svc.scan_owned_incremental()
    api.group_ids = []  # 本分片没拿到任何群
    await svc.scan_owned_incremental(group_filter=["other-group"])
    assert (await _group(store)).managed == 1


@pytest.mark.asyncio
async def test_incremental_does_not_unmanage_other_accounts_group(env):
    """L5：其它账号名下的群不归本账号管，不得被本账号的扫描收敛。"""
    store, api, svc = env
    await store.upsert_groups([GroupInfo(group_id="g9", account_id="20002")])
    api.group_ids = []
    await svc.scan_owned_incremental()
    assert (await _group(store, "g9")).managed == 1

# ---------- P2-9：一次扫描只读一次 groups 全表 ----------


class _CountingStore:
    """Delegates to a real store; counts list_groups calls."""

    def __init__(self, inner):
        self._inner = inner
        self.list_groups_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_groups(self, include_hidden: bool = False):
        self.list_groups_calls += 1
        return await self._inner.list_groups(include_hidden)


@pytest.mark.asyncio
async def test_scan_owned_reads_group_table_once(env):
    """P2-9：分片扫描路径原本把 groups 全表读两次（known_ids 与 known 是同一查询）。

    只数"扫描主体"的读：关掉 auto_label，因为 `auto_fill_labels()` 要保持无参
    签名（`webapi/groups.py` 会单独调它），它内部那次读不在本契约内。
    """
    store, api, svc = env
    await svc.scan_owned()  # 先建一行已知群，让 known 非空
    spy = _CountingStore(store)
    svc.store = spy
    svc.auto_label = False

    spy.list_groups_calls = 0
    result = await svc.scan_owned(group_filter=["g1"])

    # 反空断言：这一趟必须真的扫到了群，否则计数断言毫无意义。
    assert result.total == 1, f"本趟应扫 1 个群，实际 {result.total}"
    assert spy.list_groups_calls == 1, (
        f"扫描主体应只读 1 次 groups 全表，实际 {spy.list_groups_calls} 次"
    )
