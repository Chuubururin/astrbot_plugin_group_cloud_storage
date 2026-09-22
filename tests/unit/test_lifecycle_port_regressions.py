"""低危回归：lifecycle.init() 步骤隔离 + 端口声明 + preview_policy 别名。

1. lifecycle：``self._inited = True`` 之后只有 dlserver.start() 被 try 包裹；
   resolve_platform_bot / maybe_submit_scan / 周期任务 / bridge.recover
   未隔离。一旦其中任何一步抛错，之后 init() 全部走幂等短路 → 周期性巡检 /
   bridge 恢复 / 自动扫描再也不会启动。
2. ports/meta_store.py：``list_accounts`` 被 lifecycle 与 webapi 使用但未在
   MetaStorePort 声明（绕过 “持久化只能经端口” 约定，换实现会 AttributeError）；
   ``has_volume_part`` 形参名 part_name_pattern 与实现的 part_name_glob 不一致。
3. file_type.preview_policy_for：覆盖项的 types 匹配未走 normalize_type，
   历史别名 data / program 永远匹配不上。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

from adapters.persistence.sqlite.groups import GroupsMixin  # noqa: E402
from adapters.persistence.sqlite.volumes import VolumesMixin  # noqa: E402
from core.domain.file_type import preview_policy_for  # noqa: E402
from ports.meta_store import MetaStorePort  # noqa: E402


def _load_lifecycle():
    """按文件路径加载 core/runtime/lifecycle.py。

    ``import core.runtime.lifecycle`` 会先执行 core/runtime/__init__.py，后者
    导入 RuntimeAdapter 并依赖宿主 astrbot.core 包（测试桩下不存在）。这里绕开
    包 __init__，测的仍是生产代码本身（同 test_main_cssave_parse.py 的做法）。
    """
    path = ROOT / "core" / "runtime" / "lifecycle.py"
    spec = importlib.util.spec_from_file_location("_lifecycle_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LifecycleManager = _load_lifecycle().LifecycleManager


# ---------------- lifecycle: init() 步骤隔离 ----------------


class _FakeStore:
    async def init(self):
        return None

    async def restore_all_groups(self):
        return 0

    async def upsert_resources(self, items):
        return 0

    async def close(self):
        return None


class _FakeQueue:
    def __init__(self):
        self.starts = 0
        self.submitted = []

    async def start(self):
        self.starts += 1

    async def submit(self, kind, target=None, payload=None):
        self.submitted.append((kind, payload))

    async def shutdown(self):
        return None


class _FakeDlserver:
    def __init__(self, boom=False):
        self.boom = boom
        self.started = 0

    async def start(self):
        self.started += 1
        if self.boom:
            raise OSError("port occupied")

    async def shutdown(self):
        return None


class _FakeResolver:
    def __init__(self):
        self.bots = [object()]
        self.preferred_bot = None
        self.resolved = 0

    async def resolve_once(self):
        self.resolved += 1

    async def ensure(self, **kwargs):
        return None

    async def purge_stale_bots(self):
        return [], []

    def get_online_account_ids(self):
        return {"10001"}


class _FakeBridge:
    def __init__(self):
        self.recovered = 0

    async def recover(self):
        self.recovered += 1

    async def stop_polling(self):
        return None


def _make_lifecycle(*, dlserver_boom=False):
    task_control = SimpleNamespace(reconcile=_noop)
    kernel = SimpleNamespace(
        services=SimpleNamespace(task_control=task_control),
        track_task=lambda t: None,
        cancel_all=_noop,
    )
    resolver = _FakeResolver()
    dlserver = _FakeDlserver(boom=dlserver_boom)
    bridge = _FakeBridge()
    lm = LifecycleManager(
        kernel,
        resolver=resolver,
        store=_FakeStore(),
        queue=_FakeQueue(),
        dlserver=dlserver,
        bridge=bridge,
        config=SimpleNamespace(),
        auto_scan_hours=6,
    )
    return lm, bridge, dlserver


async def _noop(*args, **kwargs):
    return None


async def _settle():
    """让 _create_runtime_task 创建的后台 task 获得一次执行机会。"""
    await asyncio.sleep(0.05)


async def _drain(lm):
    tasks = list(lm._tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_init_continues_after_scan_submit_failure():
    """反向验证：maybe_submit_scan 抛错不得阻止周期任务/bridge 恢复启动。"""
    lm, bridge, _ = _make_lifecycle()

    async def boom():
        raise RuntimeError("scan submit boom")

    lm.maybe_submit_scan = boom
    try:
        await lm.init()
        await _settle()
        assert lm._inited is True
        assert lm._periodic_resolve_task is not None, "周期巡检必须已启动"
        assert bridge.recovered == 1, "bridge 恢复必须已启动"
        # 幂等：第二次调用不重复启动
        await lm.init()
        assert bridge.recovered == 1
    finally:
        await _drain(lm)


async def test_init_continues_after_resolve_failure():
    lm, bridge, _ = _make_lifecycle()

    async def boom():
        raise RuntimeError("resolve boom")

    lm.resolve_platform_bot = boom
    try:
        await lm.init()
        await _settle()
        assert lm._inited is True
        assert lm._periodic_resolve_task is not None
        assert bridge.recovered == 1
    finally:
        await _drain(lm)


async def test_init_continues_after_dlserver_failure():
    lm, bridge, dlserver = _make_lifecycle(dlserver_boom=True)
    try:
        await lm.init()
        await _settle()
        assert dlserver.started == 1
        assert lm._inited is True
        assert lm._periodic_resolve_task is not None
        assert bridge.recovered == 1
    finally:
        await _drain(lm)


async def test_init_happy_path_still_works():
    lm, bridge, _ = _make_lifecycle()
    try:
        await lm.init()
        await _settle()
        assert lm._inited is True
        assert lm._known_accounts == {"10001"}
        assert lm.queue.starts >= 1
        assert [k for k, _ in lm.queue.submitted] == ["scan"]
        assert lm._periodic_resolve_task is not None
        assert bridge.recovered == 1
    finally:
        await _drain(lm)


# ---------------- ports: 声明与实现一致 ----------------


def test_port_declares_list_accounts():
    """lifecycle/webapi 使用的 list_accounts 必须属于端口契约。"""
    assert hasattr(MetaStorePort, "list_accounts")
    port_sig = inspect.signature(MetaStorePort.list_accounts)
    impl_sig = inspect.signature(GroupsMixin.list_accounts)
    assert list(port_sig.parameters) == list(impl_sig.parameters)


def test_has_volume_part_param_name_matches_implementation():
    """端口形参名必须与实现一致，否则关键字调用 TypeError。"""
    port_sig = inspect.signature(MetaStorePort.has_volume_part)
    impl_sig = inspect.signature(VolumesMixin.has_volume_part)
    assert list(port_sig.parameters) == list(impl_sig.parameters)
    assert "part_name_glob" in port_sig.parameters


# ---------------- preview_policy: 别名归一 ----------------


def test_preview_policy_override_matches_legacy_alias_data():
    """{"types": "document,data"} 里的 data 必须命中 other 组。"""
    overrides = {"x": {"types": "document,data", "mode": "builtin"}}
    assert preview_policy_for("other", overrides)["mode"] == "builtin"  # data -> other
    assert preview_policy_for("document", overrides)["mode"] == "builtin"
    # 未列出的组必须保持自身默认值。archive 默认是 download（≠ override 的
    # builtin），所以一旦覆盖泄漏到未列出的组（或默认表被改错）本断言会变红；
    # 原来写 image 是恒真的：image 默认就是 builtin。
    assert preview_policy_for("archive", overrides)["mode"] == "download"


def test_preview_policy_override_matches_legacy_alias_program():
    overrides = {"x": {"types": "program", "mode": "builtin"}}
    assert preview_policy_for("installer", overrides)["mode"] == "builtin"


def test_preview_policy_override_normal_still_works():
    overrides = {
        "office": {"types": "document,spreadsheet", "mode": "external",
                   "template": "https://view.example/?url={src}"},
        "media": {"types": "video", "mode": "download"},
    }
    doc = preview_policy_for("document", overrides)
    assert doc["template"] == "https://view.example/?url={src}"
    assert preview_policy_for("video", overrides)["mode"] == "download"
    assert preview_policy_for("image", overrides)["mode"] == "builtin"
    # 空 types 不得命中任何组
    assert preview_policy_for("image", {"y": {"types": "", "mode": "download"}})["mode"] == "builtin"


# ---------------- kernel: cancel_all() 停机有界性 ----------------


def _load_kernel():
    """按文件路径加载 core/runtime/kernel.py。

    同 _load_lifecycle()：``import core.runtime.kernel`` 会先执行包 __init__，
    后者拉 RuntimeAdapter -> 宿主 astrbot.core 包（测试桩下不存在）。
    """
    path = ROOT / "core" / "runtime" / "kernel.py"
    spec = importlib.util.spec_from_file_location("_kernel_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RuntimeKernel = _load_kernel().RuntimeKernel


class _AbsorbOnce:
    """吞掉前两次取消、第三次才退出的任务（"mid-handler 只吸收标志"的最小形态）。

    **为什么是两次而不是一次**：守卫在调用 cancel_all() 之前会先自己 cancel 一次
    （证明这个桩确实吞得掉取消，否则守卫会假绿），那一次把"第一次"预支掉了。
    若桩只吞一次，注入"无界 gather"时桩在 cancel_all 里会被第二次取消打死，
    cancel_all 立刻返回 —— 守卫照样全绿（实测踩过）。
    """

    ABSORB = 2

    def __init__(self) -> None:
        self.cancels = 0
        self.quit = False

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancels += 1
                if self.quit or self.cancels > self.ABSORB:
                    raise


async def _force_finish(task, stub):
    """兜底清理，避免用例自身把任务泄漏给后续用例。"""
    stub.quit = True
    for _ in range(5):
        if task.done():
            break
        task.cancel()
        await asyncio.sleep(0.05)
    assert task.done(), "stubborn task 未能清理"


async def test_cancel_all_is_bounded_when_a_task_absorbs_cancellation():
    """有界性：吞掉取消的任务不得把 cancel_all() 拖成永久挂起。

    回归守卫：原实现 `await asyncio.gather(*self._tasks, return_exceptions=True)`
    **没有 deadline** —— 吞掉一次取消的任务永不结束，gather 也就永不返回，
    terminate()（插件停用/重载路径）直接挂死。OpQueue.shutdown() 的 S-1 是同一
    类缺陷，只是发生在 runtime kernel 上（实测：探针用 wait_for(.., 0.5) 包住
    旧实现会超时，即 bounded=False）。
    """
    kernel = RuntimeKernel(SimpleNamespace())
    stub = _AbsorbOnce()
    task = asyncio.create_task(stub.run(), name="kernel-stub")
    kernel.track_task(task)
    try:
        # 先让桩真正停泊并证明它吞得掉取消 —— 否则守卫会假绿（同队列侧守卫）。
        await asyncio.sleep(0.05)
        assert not task.done(), "stub 提前结束，守卫无效"
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "stub 未吞掉取消，守卫无效"

        t0 = time.monotonic()
        # wait_for 是本用例自己的上界：注入"无界实现"时它超时变红，而不是把
        # 整个测试进程挂死（裸环境没有 pytest-timeout 插件兜底）。
        await asyncio.wait_for(kernel.cancel_all(timeout=0.5), 2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"cancel_all 被吞取消的任务拖住: {elapsed:.3f}s"
    finally:
        await _force_finish(task, stub)


async def test_cancel_all_cancels_and_clears_tracked_tasks():
    """正路径：正常任务必须真的被取消、登记表清空。

    反空断言：没有这条，一个"什么都不做"的 cancel_all() 也能通过上一条守卫。
    """
    kernel = RuntimeKernel(SimpleNamespace())
    started = asyncio.Event()

    async def idle() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(idle(), name="kernel-idle")
    kernel.track_task(task)
    await asyncio.wait_for(started.wait(), 1.0)
    assert kernel._tasks, "登记表为空，守卫无效"

    await kernel.cancel_all(timeout=0.5)

    assert task.done() and task.cancelled(), "cancel_all 未取消已跟踪的任务"
    assert not kernel._tasks, "cancel_all 之后登记表应为空"
