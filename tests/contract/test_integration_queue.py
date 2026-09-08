"""组合集成测试（M0）：OpQueue + ResourceSyncService/GroupScanService +
OpDispatcher + SqliteMetaStore 全链路（真实组件，FakeOneBot 注入云端数据）。

覆盖评审确认的薄弱点：队列路由与业务服务组合场景。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from commands.handlers import Services  # noqa: E402
from core.application.sync import GroupScanService  # noqa: E402
from core.application.queue import OpDispatcher  # noqa: E402
from core.application.queue import Op, OpQueue  # noqa: E402
from core.application.catalog import ResourceQueryService, StatsService  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi, build_tree  # noqa: E402


async def _drain(queue: OpQueue, n: int, timeout: float = 12.0) -> dict:
    """等待 n 个 op 全部终态（recent 条数达标且无运行/积压）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        if len(st["recent"]) >= n and not st["running"] and st["depth"] == 0:
            return st
        await asyncio.sleep(0.05)
    raise TimeoutError(f"queue drain timeout: {await queue.status()}")


def _make_env(store: SqliteMetaStore, api: FakeOneBotApi):
    """构造 队列↔分发器 组合（handler 经 cell 延迟绑定解决构造环）。"""
    cell: dict = {}
    queue = OpQueue(lambda op: cell["d"].handle(op), interval=0.0,
                    backoff_base=0.05)
    sync = ResourceSyncService(api, store)
    scan = GroupScanService(api, store, queue)
    services = Services(
        permission=None, store=store, api=api, sync=sync,
        query=ResourceQueryService(store), stats=StatsService(store),
        scan=scan, queue=queue, config={"managed_groups": []},
    )
    dispatcher = OpDispatcher(
        services, api, store, sync, scan, None, None, None, queue,
        services.config, bots_getter=lambda: [],
    )
    cell["d"] = dispatcher
    return queue, dispatcher, sync, scan


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_sync_op_through_queue_and_dispatcher(store):
    """sync op 全链路：队列 → 分发器 → 同步服务 → 索引/快照落库。"""
    api = FakeOneBotApi(build_tree(file_total=300, folder_total=5, files_per_folder=20))
    queue, _, _, _ = _make_env(store, api)
    try:
        await queue.submit("sync", target="g1")
        st = await _drain(queue, 1)
        assert st["recent"][0]["state"] == "ok"
        page = await store.query_resources(ResourceQuery(group_id="g1", page_size=1000))
        assert page.total == 100
        stats = await store.stats("g1")
        assert stats.file_count == 100
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_op_through_queue_and_dispatcher(store):
    """scan op 全链路：队列 → 分发器 → 群扫描（owned 判定/容量采集/进度）。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1", "g2", "g3"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner"), GroupMember("20001", "A", "member")],
            "g2": [GroupMember("30001", "B", "owner"), GroupMember("10001", "Me", "member")],
            "g3": [GroupMember("30001", "B", "owner")],
        },
    )
    queue, _, _, scan = _make_env(store, api)
    try:
        await queue.submit("scan", target="*")
        st = await _drain(queue, 1)
        assert st["recent"][0]["state"] == "ok"
        assert scan.last_result is not None
        assert scan.last_result.total == 3
        assert scan.last_result.owned == 1  # 仅 g1（机器人为群主）
        groups = await store.list_groups()
        assert len(groups) == 3
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_rename_op_through_queue_and_dispatcher(store):
    """rename op 全链路：真实改名 + 回读校验 + 本地 display/label 回填。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner")],
        },
    )
    api.group_names = {"g1": "新名字"}  # 回读校验一致
    queue, _, _, _ = _make_env(store, api)
    try:
        await queue.submit("scan", target="*")
        await _drain(queue, 1)
        await queue.submit(
            "rename", target="g1",
            payload={"name": "新名字", "display_name": "新名字", "label": "L1"},
        )
        await _drain(queue, 2)
        assert "set_group_name:g1:新名字" in api.calls
        g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
        assert g1.display_name == "新名字"
        assert g1.label == "L1"
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_unknown_kind_fails_through_dispatcher(store):
    """未知 op kind：分发器抛出 → 队列终态 failed（错误透出）。"""
    api = FakeOneBotApi(tree={None: ([], [])})
    queue, _, _, _ = _make_env(store, api)
    try:
        await queue.submit("bogus_kind", target="g1")
        st = await _drain(queue, 1)
        assert st["recent"][0]["state"] == "failed"
        assert "unknown op kind" in (st["recent"][0].get("error") or "")
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_file_scan_streams_data_changed(store):
    """file_scan 扫描中按节流发布 data_changed（边扫边刷新文件列表/容量）。"""
    from core.domain.sync import GroupInfo

    api = FakeOneBotApi(build_tree(file_total=40, folder_total=2, files_per_folder=10))
    queue, dispatcher, _, _ = _make_env(store, api)
    events: list[dict] = []

    async def listener():
        async for ev in queue.subscribe():
            events.append(ev)

    # range 模式现在有开闸兜底：目标群必须先落库（归属账号已在线）
    await store.upsert_groups(
        [GroupInfo(group_id=g, account_id="10001") for g in
         ("g1", "g2", "g3", "g4", "g5")]
    )
    t = asyncio.create_task(listener())
    try:
        op = Op(task_id="fs1", kind="file_scan", target="*",
                payload={"mode": "range", "groups": ["g1", "g2", "g3", "g4", "g5"]})
        await dispatcher.do_file_scan(op)
        t.cancel()
        await asyncio.sleep(0.05)
        dc = [e for e in events if e.get("type") == "data_changed"]
        prog = [e for e in events if e.get("type") == "progress"]
        assert dc, "扫描中应发布 data_changed（边扫边刷新）"
        assert prog
        assert 1 <= len(dc) <= 5  # 节流：5 群内 data_changed 次数受限
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_file_scan_range_drops_not_openable_groups(store):
    """range 模式开闸兜底：队列执行时剔除归属账号离线的群，不放行云端拉取。"""
    api = FakeOneBotApi(build_tree(file_total=4, folder_total=1, files_per_folder=2))
    queue, dispatcher, _, scan = _make_env(store, api)
    try:
        await queue.submit("scan", target="*")
        await _drain(queue, 1)
        groups = {g.group_id: g for g in await store.list_groups()}
        assert groups["g1"].account_id == "10001"
        # 账号在线时 range 全部放行
        scan.set_online_ids_callback(lambda: {"10001"})
        op = Op(task_id="fs1", kind="file_scan", target="*",
                payload={"mode": "range", "groups": ["g1", "no-such"]})
        await dispatcher.do_file_scan(op)
        # 账号离线后重放：g1 被剔除（no-such 本就未受管被剔除），不触发全量拉取
        scan.set_online_ids_callback(lambda: set())
        op2 = Op(task_id="fs2", kind="file_scan", target="*",
                 payload={"mode": "range", "groups": ["g1"]})
        await dispatcher.do_file_scan(op2)
        assert not any("list_group_root" in c for c in api.calls)
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chains_per_group_file_scan(store):
    """逐群接续：群信息落库后立即提交该群的 file_scan，不等整轮群遍历。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1", "g2"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner")],
            "g2": [GroupMember("30001", "B", "owner")],
        },
    )
    queue, dispatcher, _, scan = _make_env(store, api)
    scan.on_group_scanned = dispatcher._on_group_scanned  # 生产装配等价物
    try:
        await queue.submit("scan", target="*", payload={"initial": True})
        # scan + 每群一个 file_scan
        st = await _drain(queue, 3)
        assert st["recent"][0]["state"] == "ok"
        fs = [r for r in st["recent"] if r["kind"] == "file_scan"]
        assert {r["target"] for r in fs} == {"g1", "g2"}
        # 回调已装配 → 批量兜底不再触发（无 target="*" 的 file_scan）
        assert all(r["target"] != "*" for r in fs)
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chaining_dedupes_repeated_groups(store):
    """去重：同一群在链式集合未释放前重复触发只提交一次 file_scan。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1"],
        bot_qq="10001",
        members_by_group={"g1": [GroupMember("10001", "Me", "owner")]},
    )
    queue, dispatcher, _, scan = _make_env(store, api)
    scan.on_group_scanned = dispatcher._on_group_scanned
    try:
        await queue.submit("scan", target="*")
        await _drain(queue, 2)  # scan + g1 的 file_scan
        # file_scan 已完成（dedupe 条目在 do_file_scan 开头释放）→ 再次触发可提交
        await dispatcher._on_group_scanned(
            group_id="g1", account_id="10001", is_new=False, role_determined=True
        )
        await _drain(queue, 3)
        # dedupe 生效场景：模拟未释放时重复触发
        dispatcher._chained_file_scan_groups.add("g1")
        await dispatcher._on_group_scanned(
            group_id="g1", account_id="10001", is_new=False, role_determined=True
        )
        await asyncio.sleep(0.1)
        fs = [r for r in (await queue.status())["recent"] if r["kind"] == "file_scan"]
        assert len(fs) == 2  # 第二次重复触发被去重吞掉
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chaining_skips_undetermined_new_group(store):
    """新群角色未判定（judge 失败）→ 不接续 file_scan（可能属于其他账号）。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1"],
        bot_qq="10001",
    )

    async def _fail_member_info(group_id, user_id, no_cache=False):
        raise RuntimeError("role judge failed")

    api.get_group_member_info = _fail_member_info  # 判定失败 → role 保持 unknown
    queue, dispatcher, _, scan = _make_env(store, api)
    scan.on_group_scanned = dispatcher._on_group_scanned
    try:
        await queue.submit("scan", target="*")
        await _drain(queue, 1)
        fs = [r for r in (await queue.status())["recent"] if r["kind"] == "file_scan"]
        assert fs == []
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chains_incremental_path(store):
    """scan_owned_incremental 路径同样触发逐群回调并提交 file_scan。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1", "g2"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner")],
            "g2": [GroupMember("30001", "B", "owner")],
        },
    )
    queue, dispatcher, _, scan = _make_env(store, api)
    scan.on_group_scanned = dispatcher._on_group_scanned
    try:
        # 第一轮：提交增量扫描（无 initial 标志，由回调逐群投递）
        await queue.submit("scan", target="*", payload={"mode": "incremental"})
        st = await _drain(queue, 3)  # scan + 2 × file_scan
        assert st["recent"][0]["state"] == "ok"
        fs = [r for r in st["recent"] if r["kind"] == "file_scan"]
        assert {r["target"] for r in fs} == {"g1", "g2"}
        # 批量兜底不应触发（回调已装配）
        assert all(r["target"] != "*" for r in fs)
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chaining_callback_exception_does_not_break_scan(store):
    """回调抛异常 → 扫描继续完成，不影响后续群处理。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1", "g2"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner")],
            "g2": [GroupMember("10001", "Me", "owner")],
        },
    )
    queue, dispatcher, _, scan = _make_env(store, api)
    # 注入一个第一次调用抛异常、后续正常的回调
    _call_count = 0

    async def _flaky_callback(**kwargs):
        nonlocal _call_count
        _call_count += 1
        if _call_count == 1:
            raise RuntimeError("callback boom")
        # 第二次及以后正常委托给 dispatcher
        await dispatcher._on_group_scanned(**kwargs)

    scan.on_group_scanned = _flaky_callback
    try:
        await queue.submit("scan", target="*")
        # scan + g2 的 file_scan（g1 回调异常，未提交）
        st = await _drain(queue, 2)
        assert st["recent"][0]["state"] == "ok"
        # 扫描结果正常：两个群都已扫描
        assert scan.last_result is not None
        assert scan.last_result.total == 2
        fs = [r for r in st["recent"] if r["kind"] == "file_scan"]
        assert len(fs) == 1
        assert fs[0]["target"] == "g2"
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_scan_chains_known_group_undetermined_role(store):
    """已知群 + 角色未确定 → 仍然接续 file_scan（沿用 DB 缓存角色）。"""
    from core.domain.resource import GroupMember

    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner")],
        },
    )
    queue, dispatcher, _, scan = _make_env(store, api)
    # 预置 g1 为已知群（role=owned），跳过首次判定
    await store.upsert_groups([
        __import__("core.domain.sync", fromlist=["GroupInfo"]).GroupInfo(
            group_id="g1", group_name="Test", role="owned",
            account_id="10001",
        )
    ])
    # 覆盖 get_group_member_info 使判定必然失败 → role 回退到 prev.role="owned"
    async def _fail_role(group_id, user_id, no_cache=False):
        raise RuntimeError("role judge failed")

    api.get_group_member_info = _fail_role
    scan.on_group_scanned = dispatcher._on_group_scanned
    try:
        await queue.submit("scan", target="*")
        st = await _drain(queue, 2)  # scan + g1 的 file_scan
        fs = [r for r in st["recent"] if r["kind"] == "file_scan"]
        assert len(fs) == 1
        assert fs[0]["target"] == "g1"
        # role_determined=False 但 is_new=False → 应通过守卫
        assert scan.last_result is not None
        assert scan.last_result.total == 1
    finally:
        await queue.shutdown()
