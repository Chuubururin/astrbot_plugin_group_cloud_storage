"""OpDispatcher 回归：config 取值兼容 dict（多账号扫描）/ 取消后的索引失效与通知。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.queue import Op, OpCancelError, OpDispatcher  # noqa: E402
from core.application.queue.op import cfg_value  # noqa: E402


class _FakeQueue:
    def __init__(self):
        self.events: list[dict] = []

    async def pause_check(self, op):
        if op.cancel:
            raise OpCancelError()

    async def acquire(self, mult: float = 1.0, account=None):
        return None

    async def submit(self, *args, **kwargs):
        return "chained"

    def publish(self, ev: dict) -> None:
        self.events.append(ev)


class _FakeSearchKv:
    def __init__(self):
        self.marked: list[str] = []

    def mark_dirty(self, gid: str) -> None:
        self.marked.append(gid)


class _FakeStore:
    async def list_groups(self):
        return []


class _FakeScan:
    def __init__(self, groups: list[str]):
        self.groups = groups
        self.scanned: list[tuple] = []

    async def list_page_groups(self, managed):
        return [SimpleNamespace(group_id=g) for g in self.groups]

    async def scan_owned(
        self, *, account_bot=None, api_override=None, group_filter=None, op=None
    ):
        self.scanned.append((getattr(account_bot, "_uin", None), list(group_filter or [])))
        return SimpleNamespace(total=1, failed=0)


class _FakeSync:
    """run_full_sync 桩：cancel_after 个群扫完后模拟用户点「中断」。"""

    def __init__(self, cancel_after: int | None = None):
        self.cancel_after = cancel_after
        self.calls: list[str] = []
        self.op = None

    async def run_full_sync(self, gid: str, lock):
        self.calls.append(gid)
        if self.cancel_after is not None and len(self.calls) >= self.cancel_after:
            self.op.cancel = True
        return SimpleNamespace(ok=True, error=None)

    async def run_diff_sync(self, gid: str, lock):
        """差分桩：与 run_full_sync 同样的「第 N 个群后中断」注入点。"""
        self.calls.append(gid)
        if self.cancel_after is not None and len(self.calls) >= self.cancel_after:
            self.op.cancel = True
        return SimpleNamespace(ok=True, error=None, files_removed=0)


async def _list_groups():
    return []


def _make_dispatcher(config, scan, sync, *, kv=None, queue=None, bots=(), jitters=None):
    def factory(bot, jitter):
        if jitters is not None:
            jitters.append((getattr(bot, "_uin", None), jitter))
        return SimpleNamespace(list_groups=_list_groups)

    return OpDispatcher(
        SimpleNamespace(searchkv=kv, lock_for=lambda gid: None),
        api=None,
        store=_FakeStore(),
        sync=sync,
        scan=scan,
        ingest=None,
        transfer=None,
        ops=None,
        queue=queue or _FakeQueue(),
        config=config,
        bots_getter=lambda: list(bots),
        bot_api_factory=factory,
    )


def test_op_declares_replayed_field():
    """``Op.replayed`` 必须是声明字段，而不是运行时挂上去的自由属性。

    队列在「重试」与「暂停->恢复」两条路径上都会置位它，处理器据此去重
    （album 的媒体列举、volume 的分段跳过）。此前它只靠
    ``op.replayed = True`` 动态挂载、用 ``getattr(op, "replayed", False)``
    读取：字段一旦改名或忘记赋值，default 会**静默**返回 False，去重失效
    且没有任何测试变红。本断言把「已声明 + 默认 False」钉成契约。
    """
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(Op)}
    assert "replayed" in fields, "Op 缺少 replayed 字段（去重契约会静默失效）"
    assert fields["replayed"].default is False

    op = Op(task_id="t1", kind="upload")
    assert op.replayed is False
    # 队列的置位方式必须继续可用
    op.replayed = True
    assert op.replayed is True


def test_cfg_value_reads_dict_and_object():
    """helper：dict 与对象两种配置来源都能取值。"""
    assert cfg_value({"request_interval": 0.5}, "request_interval", 1.0) == 0.5
    assert cfg_value({}, "request_interval", 1.0) == 1.0
    obj = SimpleNamespace(request_interval=2.5)
    assert cfg_value(obj, "request_interval", 1.0) == 2.5


@pytest.mark.asyncio
async def test_multi_account_scan_reads_interval_from_dict_config():
    """M8 回归：多账号扫描按 dict 配置取限速间隔。

    契约测试传的是普通 dict，而 _run_scan_for_bot 曾用属性访问
    self.config.request_interval -> 每个子任务抛 AttributeError，被
    gather(return_exceptions=True) 吞掉 -> scan 报 done 但一个群都没扫。
    """
    jitters: list = []
    scan = _FakeScan(["g1"])
    d = _make_dispatcher(
        {"managed_groups": ["g1"], "request_interval": 0.5},
        scan,
        _FakeSync(),
        bots=[SimpleNamespace(_uin="1001"), SimpleNamespace(_uin="1002")],
        jitters=jitters,
    )
    await d._dispatch(Op(task_id="t_scan", kind="scan", payload={"mode": "all"}))
    assert scan.scanned, "dict 配置下多账号扫描没有真正执行（AttributeError 被吞）"
    # 抖动以 dict 里的 0.5 为基准（±20%）；0.1 是 _assign_groups 的固定探测值
    scan_jitters = [j for _, j in jitters if j != 0.1]
    assert len(scan_jitters) == 2, jitters
    assert all(0.4 <= j <= 0.6 for j in scan_jitters), jitters


@pytest.mark.asyncio
async def test_file_scan_cancel_still_invalidates_index_and_notifies():
    """低危回归：取消 file_scan 也要清索引 + 通知前端。

    mark_dirty / data_changed 曾写在 try/finally 之外，取消时被整体跳过；
    而 handle() 的 _announce 对 file_scan 是跳过的 -> 索引陈旧、前端收不到
    刷新事件（任务页停在 pending）。
    """
    kv = _FakeSearchKv()
    queue = _FakeQueue()
    sync = _FakeSync(cancel_after=1)  # 第一个群扫完后用户中断
    d = _make_dispatcher({}, _FakeScan(["g1", "g2", "g3"]), sync, kv=kv, queue=queue)
    op = Op(task_id="t_fs", kind="file_scan", target="*", payload={"mode": "all"})
    sync.op = op
    with pytest.raises(OpCancelError):
        await d.do_file_scan(op)
    assert kv.marked == ["g1", "g2", "g3"]
    finals = [
        e for e in queue.events
        if e.get("type") == "data_changed"
        and e.get("kind") == "file_scan"
        and "i" not in e
    ]
    assert finals, queue.events


@pytest.mark.asyncio
async def test_diff_scan_cancel_still_invalidates_index_and_notifies():
    """低危回归（与 file_scan 对称）：取消 diff_file_scan 也要清索引 + 通知前端。

    do_file_scan 的 mark_dirty / data_changed 已经搬进 try/finally，取消时
    仍会执行；do_diff_scan 的同类清理却还写在循环之后 —— 一旦用户点「中断」
    （pause_check 抛 OpCancelError）就被整体跳过。而 _SELF_ANNOUNCED_KINDS
    同时含 "file_scan" 与 "diff_file_scan"，handle() 的 _announce 对两者都
    跳过，于是取消一次差分扫描 = 搜索索引陈旧 + 前端任务页停在 pending。
    """
    kv = _FakeSearchKv()
    queue = _FakeQueue()
    sync = _FakeSync(cancel_after=1)  # 第一个群差分完后用户中断
    d = _make_dispatcher({}, _FakeScan(["g1", "g2", "g3"]), sync, kv=kv, queue=queue)
    op = Op(task_id="t_ds", kind="diff_file_scan", target="*", payload={})
    sync.op = op
    with pytest.raises(OpCancelError):
        await d.do_diff_scan(op)
    assert kv.marked == ["g1", "g2", "g3"], kv.marked
    finals = [
        e for e in queue.events
        if e.get("type") == "data_changed"
        and e.get("kind") == "diff_file_scan"
        and "i" not in e
    ]
    assert finals, queue.events
