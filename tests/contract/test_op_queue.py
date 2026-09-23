"""OpQueue 限速队列测试（docs/09 §12.5）：限速、重试、SSE、取消。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.queue import Op, OpQueue  # noqa: E402


class _Recorder:
    """记录 handler 调用时间/次数，可注入失败。"""

    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self.times: list[float] = []
        self.fails = 0

    async def run(self, op: Op) -> None:
        self.times.append(time.monotonic())
        if self.fails < self.fail_first:
            self.fails += 1
            raise RuntimeError("boom")


async def _drain(queue: OpQueue, n: int, timeout: float = 8.0) -> None:
    """等待 n 个 op 全部完成（recent 中出现对应条数）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        if len(st["recent"]) >= n and not st["running"] and st["depth"] == 0:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"queue drain timeout: {await queue.status()}")


@pytest.mark.asyncio
async def test_rate_limited_execution():
    from adapters.limiter.interval import KeyedLimiter

    rec = _Recorder()
    # 限速经 RateLimiter 端口注入（bootstrap 组装 KeyedLimiter）；interval 参数仅为兼容保留
    q = OpQueue(rec.run, interval=0.0, limiter=KeyedLimiter(interval=0.06))
    await q.start()
    for _ in range(3):
        await q.submit("test")
    await _drain(q, 3)
    assert len(rec.times) == 3
    # 相邻执行间隔 ≥ interval（容差 15ms）
    gaps = [b - a for a, b in zip(rec.times, rec.times[1:], strict=False)]
    assert all(g >= 0.045 for g in gaps), gaps
    await q.shutdown()


@pytest.mark.asyncio
async def test_retry_with_backoff():
    rec = _Recorder(fail_first=2)
    q = OpQueue(rec.run, interval=0.0, max_retries=3, backoff_base=0.05)
    await q.start()
    await q.submit("test")
    await _drain(q, 1)
    assert rec.fails == 2  # 前两次失败，第三次成功（重试次数会进队列）
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_permanent_failure_marked():
    rec = _Recorder(fail_first=99)
    q = OpQueue(rec.run, interval=0.0, max_retries=2, backoff_base=0.03)
    await q.start()
    await q.submit("test")
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"
    assert rec.fails == 3  # 初始 + 2 次重试（上限）
    await q.shutdown()


@pytest.mark.asyncio
async def test_pause_visible_during_retry_backoff():
    """回归：重试回退/重排队期间任务保持可控——pause 命中且恢复后跑完。

    修复前 op 在重试窗口从 _pending/_ops_by_id 全部脱落，
    pause_task 全程返回 "unknown"，任务页的 retry 行无法暂停。
    """
    failed = asyncio.Event()
    calls = {"n": 0}

    async def run(op: Op) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            failed.set()
            raise RuntimeError("boom")

    q = OpQueue(run, interval=0.0, max_retries=3, backoff_base=0.3)
    await q.start()
    tid = await q.submit("test")
    await failed.wait()
    await asyncio.sleep(0.05)  # worker 进入 backoff sleep（_pending 已回补）
    assert q.pause_task(tid) == "queued"  # 修复前 "unknown"
    assert q.resume_task(tid) == "resumed"
    await _drain(q, 1)
    assert calls["n"] == 2  # 失败一次 + 恢复后成功一次
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_sticks():
    """回归：backoff sleep 内的取消不再被 finally 擦除——任务不会重跑。

    修复前 finally 的 _cancelled.discard 会吞掉 sleep 期间落下的取消标记，
    任务在用户取消后继续重试并完成。
    """
    failed = asyncio.Event()
    calls = {"n": 0}

    async def run(op: Op) -> None:
        calls["n"] += 1
        failed.set()
        raise RuntimeError("boom")

    q = OpQueue(run, interval=0.0, max_retries=3, backoff_base=0.3)
    await q.start()
    tid = await q.submit("test")
    await failed.wait()
    await asyncio.sleep(0.05)  # worker 进入 backoff sleep
    assert q.cancel_task(tid) is True  # 修复前 False（op 不在任何索引里）
    await _drain(q, 1)
    assert calls["n"] == 1  # 取消后不重跑（修复前会继续重试 2 次）
    st = await q.status()
    assert st["recent"][0]["state"] == "cancelled"
    await q.shutdown()


@pytest.mark.asyncio
async def test_sse_events_flow():
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.0)
    await q.start()
    events: list[str] = []

    async def listener():
        async for ev in q.subscribe():
            events.append(ev["type"])
            if ev["type"] == "done":
                return

    t = asyncio.create_task(listener())
    await asyncio.sleep(0.05)
    await q.submit("test")
    await t
    assert events == ["queued", "started", "done"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_local_error_not_retried():
    """LOCAL_ERROR（如缺 bot 上下文）属环境态：直接失败，不做指数重试。"""
    from core.domain.enums import OneBotApiError, OneBotErrorKind

    async def fail(op):
        raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, "scan", "no bot")

    q = OpQueue(fail, interval=0.0, max_retries=5, backoff_base=0.02)
    await q.start()
    await q.submit("scan")
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "failed"          # 未重试、直接失败
    assert "local_error" in st["recent"][0]["error"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_skips_queued_op():
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.2)  # 限速让第 2 个任务留在队列
    await q.start()
    await q.submit("first")
    tid2 = await q.submit("second")
    # second 已出队进入限速等待（running）→ 取消位生效，等待后跳过执行
    assert q.cancel_task(tid2) is True
    await _drain(q, 2)
    assert len(rec.times) == 1  # 仅 first 执行（second 在限速等待中被取消）
    await q.shutdown()


async def _blocking_handler(gate: asyncio.Event):
    async def run(op: Op) -> None:
        await gate.wait()
    return run


@pytest.mark.asyncio
async def test_custom_high_priority_set():
    """自定义 high_priority：集合内 kind 走高优队列，集合外走常规队列。"""
    gate = asyncio.Event()
    q = OpQueue(await _blocking_handler(gate), interval=0.0,
                high_priority={"hi_kind"})
    await q.start()
    await q.submit("hi_kind")   # 高优 worker 阻塞
    await q.submit("lo_kind")   # 常规 worker 阻塞
    await asyncio.sleep(0.05)
    await q.submit("hi_kind")
    await q.submit("lo_kind")
    st = await q.status()
    assert st["high"] == 1      # 第二个 hi_kind 在高优队列
    assert st["depth"] == 2
    assert st["high_priority_kinds"] == ["hi_kind"]
    gate.set()
    await _drain(q, 4)
    await q.shutdown()


@pytest.mark.asyncio
async def test_default_high_priority_behavior_unchanged():
    """默认集合：rename 走高优，scan 走常规（内置行为不因配置化而变化）。"""
    gate = asyncio.Event()
    q = OpQueue(await _blocking_handler(gate), interval=0.0)
    await q.start()
    await q.submit("rename")    # 高优 worker 阻塞
    await q.submit("scan")      # 常规 worker 阻塞
    await asyncio.sleep(0.05)
    await q.submit("scan")
    st = await q.status()
    assert st["high"] == 0      # 第二个 scan 在常规队列
    assert st["depth"] == 1
    gate.set()
    await _drain(q, 3)
    await q.shutdown()

@pytest.mark.asyncio
async def test_keyed_limiter_accounts_parallel():
    """v2.11：键控限速——不同账号并行不受彼此节奏阻塞；同账号串行。"""
    from adapters.limiter.interval import KeyedLimiter

    lim = KeyedLimiter(interval=0.2)
    t0 = time.monotonic()
    await asyncio.gather(lim.acquire(account="A"), lim.acquire(account="B"))
    # 双账号各拿一次：几乎同时（不互等 0.2s）
    assert time.monotonic() - t0 < 0.12
    # 同账号连续两次需间隔 ≥ interval
    t0 = time.monotonic()
    await lim.acquire(account="A")
    await lim.acquire(account="A")
    assert time.monotonic() - t0 >= 0.18


@pytest.mark.asyncio
async def test_queue_per_account_concurrency():
    """v2.11：队列按账号并发消费——A/B 账号 ops 同时执行（非全局串行）。"""
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.3, slots=4)
    await q.start()
    await q.submit("t1", account="A")
    await q.submit("t2", account="B")
    await _drain(q, 2)
    # 两账号并行：第二个 op 不等待 0.3s 全局限速
    assert len(rec.times) == 2
    assert abs(rec.times[0] - rec.times[1]) < 0.25
    await q.shutdown()


@pytest.mark.asyncio
async def test_bulk_kinds_skip_pacing():
    """v2.11：重活分流——非交互重活不占全局限速节奏。"""
    rec = _Recorder()
    q = OpQueue(rec.run, interval=0.4, slots=4)
    await q.start()
    await q.submit("convert_volumes")
    await q.submit("t2")
    await _drain(q, 2)
    # convert_volumes 为 BULK：两者均立即执行（无 0.4s 串行等待）
    assert len(rec.times) == 2
    assert abs(rec.times[0] - rec.times[1]) < 0.2
    await q.shutdown()


# ---------- 状态机回归（H1/H2/H3） ----------

@pytest.mark.asyncio
async def test_cancel_queued_op_fires_single_terminal_event():
    """H1 回归：排队取消的终态只发一次。

    cancel_task 对深队列任务立即写穿终态，出队时 _execute 的 cancelled 分支
    曾重复发送 cancelled 事件/记录（前端收到两次取消、recent 双条）。
    """
    gate = asyncio.Event()
    events: list[str] = []

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, interval=0.0, slots=4)  # 2 个高优 worker
    orig_push = q._push

    def push(ev: dict) -> None:
        if ev["type"] in ("done", "failed", "cancelled"):
            events.append(f"{ev['type']}:{ev['task_id']}")
        orig_push(ev)

    q._push = push
    await q.start()
    await q.submit("move_file")            # 高优 worker 1 阻塞
    await q.submit("move_file")            # 高优 worker 2 阻塞
    await asyncio.sleep(0.05)
    victim = await q.submit("move_file")   # 留在高优队列
    # T9：先确定性断言 victim 仍在排队。原用例只靠 sleep(0.05) 抢占 2 个高优
    # worker，慢机器上 victim 可能已被出队（走 running 分支），而两条路径都恰好
    # 只发一次 cancelled —— H1 的双发路径根本没被覆盖。pause_task 对排队 op 返回
    # "queued"、对已出队的 op 返回 "running"，用它钉住路径。
    assert q.pause_task(victim) == "queued"
    assert q.resume_task(victim) == "resumed"   # 释放暂停位，回到纯排队取消路径
    assert victim in q._pending and victim not in q._running
    assert q.cancel_task(victim) is True
    # 深队列写穿已发生（running 分支的取消不会 pop）：确认走的就是 H1 路径
    assert victim not in q._ops_by_id
    gate.set()
    await _drain(q, 2)
    hits = [e for e in events if e == f"cancelled:{victim}"]
    assert len(hits) == 1, f"expected one terminal, got {events}"
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_running_then_retriable_error_converges_to_cancelled():
    """H2 回归：运行中取消 + 可重试异常 → 不再产生幽灵任务。

    重试分支把 op 放回 _pending 并重新入队，但 finally 无条件
    _cancelled.discard 抹掉了取消标记；再次出队时取消分支不命中，落到
    `if op.cancel: return`（在 try 之外、无 finally）——既不写终态也不 pop
    _ops_by_id。台账停在 retry，任务页永远显示在跑，has_pending() 永久为真，
    cancel_task 再也无法收敛该行。
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}
    terminals: list[str] = []

    async def run(op: Op) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release.wait()
            raise RuntimeError("boom")   # 普通异常：队列当作可重试

    q = OpQueue(run, interval=0.0, max_retries=3, backoff_base=0.05)
    orig_push = q._push

    def push(ev: dict) -> None:
        if ev["type"] in ("done", "failed", "cancelled"):
            terminals.append(f"{ev['type']}:{ev['task_id']}")
        orig_push(ev)

    q._push = push
    await q.start()
    tid = await q.submit("test", payload={"k": "v"})
    await started.wait()
    assert q.cancel_task(tid) is True   # 运行中取消：op.cancel=True + _cancelled
    release.set()                        # 触发可重试异常 → 重试分支重新入队
    # 有界等待收敛：修复前重试行被静默丢弃，_ops_by_id 永久残留（幽灵）
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and tid in q._ops_by_id:
        await asyncio.sleep(0.02)
    assert tid not in q._ops_by_id, "取消后的重试任务必须收敛（修复前是幽灵）"
    st = await q.status()
    assert st["recent"][0]["state"] == "cancelled"   # 修复前停在 retry，无终态
    assert calls["n"] == 1                            # 取消后不再重跑 handler
    assert q.has_pending("test", "k", "v") is False  # 修复前 True：幽灵卡住去重
    assert terminals == [f"cancelled:{tid}"]          # 终态只发一次（无重复 SSE）
    assert q.cancel_task(tid) is False                # 已不在任何索引，无法再收敛
    await q.shutdown()


@pytest.mark.asyncio
async def test_pause_resume_marks_replay_without_eating_retry_budget():
    """M2 回归：暂停→恢复重入 handler 必须带重放判据，且不吃重试预算。

    恢复后 handler 从第一行重跑，但 op.retries 仍为 0；只按 `op.retries > 0`
    判重的 handler（album 判重 / volume 分片跳过）会失效并重复产生副作用。
    这里用独立的 op.replayed 标记，并验证 retries 未被暂停路径占用。
    """
    seen: list[tuple[int, bool]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(op: Op) -> None:
        seen.append((op.retries, bool(getattr(op, "replayed", False))))
        if len(seen) == 1:
            started.set()
            await release.wait()
            await q.pause_check(op)      # 检查点暂停 → OpPausedError
        elif len(seen) == 2:
            raise RuntimeError("boom")   # 恢复重入后失败一次 → 真实重试 1 次

    q = OpQueue(run, interval=0.0, max_retries=2, backoff_base=0.05)
    await q.start()
    tid = await q.submit("test")
    await started.wait()
    assert q.pause_task(tid) == "running"
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and tid not in q._paused:
        await asyncio.sleep(0.02)
    assert tid in q._paused
    assert q.resume_task(tid) == "resumed"
    await _drain(q, 2)   # paused 记录 + 最终 ok
    assert len(seen) == 3, seen
    assert seen[0] == (0, False)   # 首次进入：不是重放
    assert seen[1][1] is True      # 恢复重入：必须是重放（修复前 False）
    assert seen[1][0] == 0         # 暂停路径不消耗重试预算（自增 retries 则为 1）
    assert seen[2] == (1, True)    # 只有真实重试吃了 1 次预算
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_cancel_running_without_checkpoint_records_cancelled():
    """H2 回归：运行中任务无暂停检查点、取消后仍跑完——终态必须收敛为
    cancelled，而不是谎报 done/ok（与 interrupt 的 "interrupted" 响应矛盾）。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(op: Op) -> None:
        started.set()
        await release.wait()  # 整段执行无 pause_check 检查点

    q = OpQueue(run, interval=0.0)
    await q.start()
    tid = await q.submit("convert_volumes")
    await started.wait()
    assert q.cancel_task(tid) is True
    release.set()  # handler 无检查点，直接跑完全程
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "cancelled"  # 修复前为 "ok"
    await q.shutdown()


@pytest.mark.asyncio
async def test_pause_running_then_immediate_resume_not_lost():
    """H3 回归：运行中暂停后立即恢复（handler 尚未到检查点）。

    pause_task 只置 op.pause、不登记 _paused，resume_task 曾返回 "unknown"
    丢失这次恢复点击，任务停在检查点需用户再点一次。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(op: Op) -> None:
        started.set()
        await release.wait()
        await q.pause_check(op)  # 检查点：暂停位未清除则此处抛出并挂起

    q = OpQueue(run, interval=0.0)
    await q.start()
    tid = await q.submit("move_file")
    await started.wait()
    assert q.pause_task(tid) == "running"
    assert q.resume_task(tid) == "resumed"  # 修复前 "unknown"
    release.set()
    await _drain(q, 1)
    st = await q.status()
    assert st["recent"][0]["state"] == "ok"
    await q.shutdown()


# ---------- 断点恢复认领（claim，item 8） ----------

class _LedgerSpy:
    """Records every ledger state the queue writes."""

    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []

    async def on_state(self, task_id, kind, target, payload, state, error=None):
        self.states.append((task_id, state))

    async def on_op(self, task_id, action, before=None, after=None):
        pass


@pytest.mark.asyncio
async def test_claim_adopts_the_row_id_and_writes_no_ledger_state():
    """认领复用原 task_id，且认领本身不是状态转移。

    submit() 总是 mint 新 id，而台账按 task_id upsert：旧版每点一次断点恢复
    就把原 pending 行丢成孤儿（进程存续期内可恢复集合单调增长）。claim() 保留
    行自身的身份，让终态写回落到用户点击的那一行。预写 "running" 则正是 Bug-13
    的僵尸形态——从未被 worker 消费的认领会留下一条永远运行中的记录。"""
    gate = asyncio.Event()
    led = _LedgerSpy()

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, interval=0.0, slots=4, ledger=led)
    await q.start()
    await q.submit("move_file")            # 高优 worker 1 阻塞
    await q.submit("move_file")            # 高优 worker 2 阻塞
    await asyncio.sleep(0.05)
    tid = await q.claim("bp-row-1", "convert_volumes", "g1", {"resource_id": "g1:file:77"})
    assert tid == "bp-row-1"                       # 原身份，未 mint 新 id
    assert tid in q._pending and tid in q._ops_by_id  # 排队中，未被消费
    assert q._ops_by_id[tid].payload == {"resource_id": "g1:file:77"}
    assert [t for t, _ in led.states if t == "bp-row-1"] == []  # 认领零台账写
    gate.set()
    await _drain(q, 3)
    assert [s for t, s in led.states if t == "bp-row-1"] == ["running", "done"]
    await q.shutdown()


@pytest.mark.asyncio
async def test_claim_refuses_an_id_the_queue_still_owns():
    """队列已持有该 id 时认领返回 None：不再让一个身份挂两套云端写入。"""
    gate = asyncio.Event()
    led = _LedgerSpy()

    async def run(op: Op) -> None:
        await gate.wait()

    q = OpQueue(run, interval=0.0, slots=4, ledger=led)
    await q.start()
    live = await q.submit("convert_volumes", "g1", {"steps": 5})
    # 拒绝路径无 await，worker 无从插队：前后快照必须逐条相等
    before = list(led.states)
    assert await q.claim(live, "convert_volumes", "g1", {}) is None
    assert led.states == before
    gate.set()
    await _drain(q, 1)
    await q.shutdown()
