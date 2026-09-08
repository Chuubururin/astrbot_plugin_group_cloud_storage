"""GroupScanService 测试（docs/09 §12.4）：owned 判定 / 进度 / Page 可管理清单 / 批量改名。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.resource import GroupMember  # noqa: E402
from core.application.sync import GroupScanService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=["g1", "g2", "g3"],
        bot_qq="10001",
        members_by_group={
            "g1": [GroupMember("10001", "Me", "owner"), GroupMember("20001", "A", "member")],
            "g2": [GroupMember("30001", "B", "owner"), GroupMember("10001", "Me", "member")],
            "g3": [GroupMember("30001", "B", "owner")],  # 机器人不在成员列表（异常兜底）
        },
    )
    queue = OpQueue(lambda op: None, interval=0.0)
    svc = GroupScanService(api, store, queue)
    yield store, api, svc
    await queue.shutdown()
    await store.close()


@pytest.mark.asyncio
async def test_scan_owned_detection(env):
    store, api, svc = env
    result = await svc.scan_owned(include_capacity=True)
    assert result.total == 3
    assert result.owned == 1  # 仅 g1（机器人为群主）
    groups = await store.list_groups()
    role_map = {g.group_id: g.role for g in groups}
    assert role_map == {"g1": "owned", "g2": "member", "g3": "member"}
    # 容量采集中（fake fs_info 默认 10GB）
    g1 = next(g for g in groups if g.group_id == "g1")
    assert g1.total_space > 0 and g1.used_space >= 0
    # 增量判定（效率修复）：首扫判定 3 群
    assert sum(1 for c in api.calls if c.startswith("get_group_member_info")) == 3
    # 二次扫描：无新群 → 不再调用成员判定（仅容量/列表）
    api.calls.clear()
    r2 = await svc.scan_owned()
    assert r2.owned == 1
    assert sum(1 for c in api.calls if c.startswith("get_group_member_info")) == 0


@pytest.mark.asyncio
async def test_page_managed_whitelist_union_owned(env):
    store, api, svc = env
    await svc.scan_owned()
    # 白名单非空 → 白名单 ∪ owned
    managed = await svc.list_page_groups(["g2", "799"])
    ids = {g.group_id for g in managed}
    assert ids == {"g1", "g2", "799"}   # owned(g1) + 白名单(g2、799 未扫描补入)
    # 不在白名单且非 owned → 不可管理
    assert await svc.is_page_managed("g3", ["g2"]) is False
    # 白名单扩展示例（mentioned 群）
    assert await svc.is_page_managed("799", ["799"]) is True


@pytest.mark.asyncio
async def test_new_group_triggers_judgement_only(env):
    """增量策略：新增群出现时才做 owned 判定。"""
    store, api, svc = env
    await svc.scan_owned()
    api.calls.clear()
    api.group_ids = ["g1", "g2", "g3", "g9"]  # 新群 g9
    api.members_by_group["g9"] = [GroupMember("10001", "Me", "owner")]  # 机器人是 g9 群主
    api.bot_qq = "10001"
    r = await svc.scan_owned()
    assert r.owned == 2  # g1 + g9
    judged = [c for c in api.calls if c.startswith("get_group_member_info")]
    assert judged == ["get_group_member_info:g9"]  # 仅新群判定


@pytest.mark.asyncio
async def test_auto_label_after_scan(env):
    """自动标号：扫描后自动为未标号群续填编号（已有标号保留）。"""
    store, api, svc = env
    await svc.scan_owned()
    groups = {g.group_id: g for g in await store.list_groups()}
    # 3 个群无标号 → 自动填 A/B/C（按 sort_order/group_id 序）
    labels = sorted((groups[g].label or "" for g in groups))
    assert labels == ["A", "B", "C"]
    # 已有标号保留：给 g2 手动设 X，g3 保持 C
    await store.update_group_fields("g2", label="X")
    api.calls.clear()
    await svc.scan_owned()
    groups = {g.group_id: g for g in await store.list_groups()}
    assert groups["g2"].label == "X" and groups["g3"].label == "C"
    # 新增群 g9（无标号）→ 扫描后自动续填下一个未占用编号 D
    api.group_ids = ["g1", "g2", "g3", "g9"]
    api.calls.clear()
    await svc.scan_owned()
    groups = {g.group_id: g for g in await store.list_groups()}
    # 续号取最小未占用：A/C/X 占用、B 已释放（g2 换标号后）→ g9 得 B
    assert groups["g9"].label == "B"


@pytest.mark.asyncio
async def test_auto_label_disabled(env):
    store, api, svc = env
    svc.auto_label = False
    await svc.scan_owned()
    groups = await store.list_groups()
    assert all(g.label is None for g in groups)


@pytest.mark.asyncio
async def test_remove_managed_hides_and_no_resurrect(env):
    """移除管理：managed=0 从列表隐藏；扫描不复活。"""
    store, api, svc = env
    await svc.scan_owned()
    # 移除 g2（member 群）
    await store.set_groups_managed(["g2"], 0)
    allowed = await svc.list_page_groups([])
    assert "g2" not in {g.group_id for g in allowed}
    assert "g1" in {g.group_id for g in allowed}
    # 再次扫描：g2 不复活（managed 保留 0）
    await svc.scan_owned()
    allowed = await svc.list_page_groups([])
    assert "g2" not in {g.group_id for g in allowed}
    # 恢复
    await store.set_groups_managed(["g2"], 1)
    allowed = await svc.list_page_groups([])
    assert "g2" in {g.group_id for g in allowed}


@pytest.mark.asyncio
async def test_pick_group_owned_first_and_capacity(env):
    """上传自动选群：owned 优先 + 余量不足跳过（溢出切换依据）。"""
    store, api, svc = env
    from core.application.catalog import StoragePlanner
    from core.domain.resource import GroupMember as _gm

    planner = StoragePlanner(store)
    # 无容量数据时按 owned 优先
    groups = [
        __import__("core.domain.sync", fromlist=["GroupInfo"]).GroupInfo(
            group_id="a", role="member", sort_order=1),
        __import__("core.domain.sync", fromlist=["GroupInfo"]).GroupInfo(
            group_id="b", role="owned", sort_order=2),
    ]
    pick = await planner.pick_group(groups)
    assert pick.group_id == "b"
    # 容量不足跳过
    groups[1].used_space = 9_999_999_000
    groups[1].total_space = 10_000_000_000
    groups[0].used_space = 0
    groups[0].total_space = 10_000_000_000
    pick2 = await planner.pick_group(groups, requested_bytes=100 * 1024 * 1024)
    assert pick2.group_id == "a"  # owned 余量不足 → 落到 member


@pytest.mark.asyncio
async def test_empty_managed_allows_all(env):
    """订正：白名单空 → Page 放行所有已扫描群。"""
    store, api, svc = env
    await svc.scan_owned()
    allowed = await svc.list_page_groups([])
    assert {g.group_id for g in allowed} == {"g1", "g2", "g3"}
    assert await svc.is_page_managed("g2", []) is True
    assert await svc.is_page_managed("unknown_group", []) is True


# ---------- 开闸校验（离线账号操作者 / 已解散群防护） ----------

@pytest.mark.asyncio
async def test_group_open_gate_offline_owner(env):
    """归属账号离线 → open gate 拒绝（群归属账号离线）。"""
    store, api, svc = env
    await svc.scan_owned()
    # g1 归属 bot_qq=10001；回调返回空集 → 该账号离线
    svc.set_online_ids_callback(lambda: set())
    with pytest.raises(ValueError, match="离线"):
        await svc.assert_group_openable("g1", [])
    # 账号在线 → 通过离线检查，进入远端解散探测（fake get_group_info 有名字 → 放行）
    svc.set_online_ids_callback(lambda: {"10001"})
    await svc.assert_group_openable("g1", [])


@pytest.mark.asyncio
async def test_group_open_gate_dissolved_group(env):
    """账号在线但群已消失（get_group_info 无名字）→ fail-closed 拒绝。"""
    store, api, svc = env
    await svc.scan_owned()
    svc.set_online_ids_callback(lambda: {"10001"})
    # 模拟解散：远端群信息不可得（空 group_name）
    async def _gone(group_id, no_cache=False):
        return {"group_id": group_id, "group_name": ""}
    api.get_group_info = _gone
    with pytest.raises(ValueError, match="解散"):
        await svc.assert_group_openable("g1", [])


@pytest.mark.asyncio
async def test_group_open_gate_remote_error_fail_closed(env):
    """远端探测抛错（协议层失败）→ 同样按解散 fail-closed 拒绝。"""
    store, api, svc = env
    await svc.scan_owned()
    svc.set_online_ids_callback(lambda: {"10001"})
    async def _boom(group_id, no_cache=False):
        raise RuntimeError("api unreachable")
    api.get_group_info = _boom
    with pytest.raises(ValueError, match="解散"):
        await svc.assert_group_openable("g1", [])


@pytest.mark.asyncio
async def test_group_open_gate_unmanaged_group(env):
    """未受管（未知群）→ group not managed 拒绝，且不发起远端探测。"""
    store, api, svc = env
    await svc.scan_owned()
    svc.set_online_ids_callback(lambda: {"10001"})
    calls = {"n": 0}
    async def _probe(group_id, no_cache=False):
        calls["n"] += 1
        return {"group_id": group_id, "group_name": "x"}
    api.get_group_info = _probe
    with pytest.raises(ValueError, match="not managed"):
        await svc.assert_group_openable("no-such-group", ["g2"])
    assert calls["n"] == 0  # 短路：未受管不做远端调用


@pytest.mark.asyncio
async def test_list_page_groups_filters_offline_accounts(env):
    """白名单非空时离线账号的群也不列出（白名单优先复活仅限在线账号）。"""
    store, api, svc = env
    await svc.scan_owned()
    svc.set_online_ids_callback(lambda: set())  # 全部离线
    allowed = await svc.list_page_groups(["g2", "799"])
    assert {g.group_id for g in allowed} == {"799"}  # 无归属的未知白名单项保留
    # 恢复在线 → g1/g2 回到列表（数据未删除）
    svc.set_online_ids_callback(lambda: {"10001"})
    allowed = await svc.list_page_groups(["g2", "799"])
    assert {g.group_id for g in allowed} == {"g1", "g2", "799"}


@pytest.mark.asyncio
async def test_rename_remote_verify_backfill(env):
    """校验后回填：API 成功且群名一致 → 写本地；不一致 → 抛错且不写。"""
    store, api, svc = env
    await svc.scan_owned()
    # 一致：fake 群名跟随改名（模拟 NapCat 回读）
    api.group_names = {"g1": "研发齐心协力"}
    await svc.rename_remote("g1", "研发齐心协力", display_name="研发齐心协力", label="A")
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.shown_name == "研发齐心协力" and g1.label == "A"
    # 不一致：校验失败抛错，本地不写
    import pytest as _pytest

    with _pytest.raises(Exception) as ei:
        await svc.rename_remote("g1", "另一个名字", display_name="另一个名字")
    assert "mismatch" in str(ei.value)
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.shown_name == "研发齐心协力"  # 未填充

@pytest.mark.asyncio
async def test_incremental_collects_album_and_essence(env):
    """v8：增量扫描对新群采集相册/精华数量。"""
    store, api, svc = env
    api.albums = {"g1": [{"album_id": "a1"}, {"album_id": "a2"}]}
    api.essences = {"g1": [{"message_id": 1}]}
    await svc.scan_owned_incremental(include_capacity=True)
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.album_count == 2
    assert g1.essence_count == 1
    # 存量已知群（再次增量）不重采（调用次数不变）
    before = [c for c in api.calls if c.startswith("get_qun_album_list:g1")]
    await svc.scan_owned_incremental(include_capacity=True)
    after = [c for c in api.calls if c.startswith("get_qun_album_list:g1")]
    assert len(after) == len(before)  # 增量不重采已知群


@pytest.mark.asyncio
async def test_album_essence_resourceized(env):
    """v9：增量扫描将相册条目与精华消息资源化落库（统一资源目录）。"""
    store, api, svc = env
    api.albums = {"g1": [
        # 真实 NapCat 相册条目字段（name/owner/desc/upload_number）
        {"album_id": "a1", "name": "旅行", "owner": "7", "desc": "出差随拍",
         "create_time": "100", "upload_number": "3"},
    ]}
    api.essences = {"g1": [
        # 真实 NapCat 段数组格式（content 为消息段列表，非字符串）
        {"message_id": "m1",
         "content": [{"type": "text", "data": {"text": "重要通知：本周五发布"}},
                     {"type": "face", "data": {"id": "1"}}],
         "sender_id": "7", "time": 200},
        # 纯图片/视频段 → 名称归一为 [图片/视频]
        {"message_id": "m2",
         "content": [{"type": "image", "data": {"file": "x.jpg"}}],
         "sender_id": "7", "time": 201},
    ]}
    await svc.scan_owned_incremental(include_capacity=True)
    from core.domain.sync import ResourceQuery
    page = await store.query_resources(ResourceQuery(group_id="g1", type="album"))
    assert page.total == 1 and page.items[0].name == "旅行"
    page2 = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert page2.total == 2
    names = {it.name for it in page2.items}
    assert "重要通知：本周五发布" in names and "[图片/视频]" in names
    t = next(it for it in page2.items if it.name == "重要通知：本周五发布")
    assert (t.meta or {}).get("summary")


@pytest.mark.asyncio
async def test_multi_account_scan_binding(env):
    """v9：多账号扫描——account_bot 绑定后归属 account_id。"""
    store, api, svc = env
    bot = object()
    api.account_probe = []
    api.with_bot = lambda b: api.account_probe.append(b)
    await svc.scan_owned_incremental(account_bot=bot)
    assert api.account_probe == [bot]
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.account_id == "10001"  # fake bot_qq 归属


@pytest.mark.asyncio
async def test_batch_group_ops(env):
    """v1.4：批量群操作（改名/加群方式/备注）逐群真实调用 + 本地回填。"""
    store, api, svc = env
    from core.application.queue import Op
    from core.domain.sync import GroupInfo

    await store.upsert_groups([
        GroupInfo(group_id="g1", group_name="G1"),
        GroupInfo(group_id="g2", group_name="G2"),
    ])
    op = Op(task_id="batch1", kind="batch_groups", target="*",
            payload={"action": "add_option", "value": 2,
                     "group_ids": ["g1", "g2"]})
    await svc.run_batch_ops(op)
    assert api.calls.count("set_group_add_option:g1:2") == 1
    assert api.calls.count("set_group_add_option:g2:2") == 1

    op2 = Op(task_id="batch2", kind="batch_groups", target="*",
             payload={"action": "remark", "value": "备注A",
                      "group_ids": ["g1"]})
    await svc.run_batch_ops(op2)
    assert "set_group_remark:g1:备注A" in api.calls
    g1 = next(g for g in await store.list_groups() if g.group_id == "g1")
    assert g1.shown_name == "备注A"

    with pytest.raises(ValueError):
        await svc.run_batch_ops(Op(
            task_id="batch3", kind="batch_groups", target="*",
            payload={"action": "add_option", "value": 9, "group_ids": ["g1"]}))


@pytest.mark.asyncio
async def test_scan_streams_groups_progressively(tmp_path):
    """边扫边落库（v2.1）：每扫一群即持久化，扫描完成前数据已在库中可见。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    gids = [f"g{i}" for i in range(1, 6)]
    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=gids,
        bot_qq="10001",
        members_by_group={g: [GroupMember("10001", "Me", "owner")] for g in gids},
    )
    queue = OpQueue(lambda op: None, interval=0.0)
    svc = GroupScanService(api, store, queue)

    # 间谍：每次落库时读库，验证「扫描中途」数据已可见（第 2 次调用时第 1 群已在库）
    visibility = []
    real_upsert = store.upsert_groups

    async def spy(items):
        rows = await store.list_groups()
        visibility.append((len(items), len(rows)))
        await real_upsert(items)

    store.upsert_groups = spy

    events = []
    async def listener():
        async for ev in queue.subscribe():
            events.append(ev)

    t = asyncio.create_task(listener())
    try:
        await svc.scan_owned()
        t.cancel()
        await asyncio.sleep(0.05)
        assert len(visibility) == 5               # 逐群各一次落库（非最后批量一次）
        assert all(n == 1 for n, _ in visibility)
        assert visibility[1][1] >= 1              # 扫描中途：库中已有先前群
        types = {e["type"] for e in events}
        assert "progress" in types and "data_changed" in types
        assert len(await store.list_groups()) == 5
    finally:
        await queue.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_guarded_call_timeout(tmp_path):
    """逐调用超时保护：单群 API 挂起不拖死整轮扫描（超时按异常抛出）。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])}, group_ids=["g1"])
    queue = OpQueue(lambda op: None, interval=0.0)
    svc = GroupScanService(api, store, queue)
    try:
        # 快速调用：正常返回
        v = await svc._with_timeout(asyncio.sleep(0.01, result=42), timeout=1.0)
        assert v == 42
        # 挂起调用：超时抛 TimeoutError
        with pytest.raises(TimeoutError):
            await svc._with_timeout(asyncio.sleep(5.0), timeout=0.1)
    finally:
        await queue.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_removed_group_reappears_via_whitelist(tmp_path):
    """v2.7.1：移除管理的群经受管群白名单复活（白名单优先语义）。"""
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    gids = ["g1", "g2"]
    api = FakeOneBotApi(
        tree={None: ([], [])},
        group_ids=gids,
        bot_qq="10001",
        members_by_group={g: [GroupMember("10001", "Me", "owner")] for g in gids},
    )
    queue = OpQueue(lambda op: None, interval=0.0)
    svc = GroupScanService(api, store, queue)
    try:
        await svc.scan_owned()
        await store.set_groups_managed(["g1"], 0)
        # 无白名单：g1 隐藏
        pg = await svc.list_page_groups([])
        assert all(g.group_id != "g1" for g in pg)
        # 白名单含 g1：复活（managed=0 也可见/可管理）
        pg2 = await svc.list_page_groups(["g1"])
        assert any(g.group_id == "g1" for g in pg2)
        assert await svc.is_page_managed("g1", ["g1"]) is True
        # 恢复端点语义：managed 置 1 后无需白名单也可见
        await store.set_groups_managed(["g1"], 1)
        pg3 = await svc.list_page_groups([])
        assert any(g.group_id == "g1" for g in pg3)
    finally:
        await queue.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_capacity_of_cloud_first_and_local_fallback(env):
    """容量口径核对（云端优先 → 本地索引兜底）：
    - 云端成功且 total>0：used/count/limit 透传；used=0 时本地索引兜底
    - 云端 total=0 或异常：返回 None（调用方跳过写入，保留上次值）
    """
    store, api, svc = env
    from core.domain.resource import FileSystemInfo

    q = svc.queue
    try:
        # 1) 云端成功（全字段）
        api.fs_info = FileSystemInfo(
            file_count=42, limit_count=1000, used_space=2048, total_space=10 * 1024 ** 3
        )
        used, total, count, limit = await svc._capacity_of("g1")
        assert (used, total, count, limit) == (2048, 10 * 1024 ** 3, 42, 1000)

        # 2) 云端 used=0 但 total>0 → 字段级本地索引兜底
        api.fs_info = FileSystemInfo(
            file_count=0, limit_count=0, used_space=0, total_space=10 * 1024 ** 3
        )
        from core.domain.resource import Resource, ResourceType

        await store.upsert_resources(
            [
                Resource(
                    group_id="g1", type=ResourceType.FILE, name="a.bin",
                    source_ref="a.bin", size=100,
                ),
                Resource(
                    group_id="g1", type=ResourceType.FILE, name="b.bin",
                    source_ref="b.bin", size=200,
                ),
            ]
        )
        used2, total2, count2, limit2 = await svc._capacity_of("g1")
        assert used2 == 300 and count2 == 2 and total2 == 10 * 1024 ** 3

        # 3) 云端 total=0 → 返回 None（调用方跳过写入）
        api.fs_info = FileSystemInfo(
            file_count=0, limit_count=0, used_space=0, total_space=0
        )
        result_none = await svc._capacity_of("g1")
        assert result_none is None

        # 4) 云端异常 → 返回 None（调用方跳过写入，保留上次值）
        async def boom(_gid):
            raise RuntimeError("fs api down")

        api.get_group_fs_info = boom
        result_exc = await svc._capacity_of("g1")
        assert result_exc is None
    finally:
        await q.shutdown()
        await store.close()
