"""core/task_cancel.cancel_tasks 单元测试（P3b）。

OpQueue.shutdown 与 RuntimeKernel.cancel_all 都押注在"绝不让调用方阻塞超过
timeout"上；历史上的两类假界（sleep 式宽限、CPython<=3.11 的 wait_for）都在这
个原语上翻过车（见模块 docstring），这里把语义钉死：
1) 空集合立即返回；
2) 停泊在可取消点的任务在几个切片内结束，不耗尽 timeout；
3) 扛住取消的任务在 deadline 被放弃并原样返回（真实上界）；
4) 混合场景只返回存活者。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.task_cancel import cancel_tasks


async def _parked(stop: asyncio.Event) -> None:
    await stop.wait()


async def _stubborn(stop: asyncio.Event) -> None:
    """吞掉每一次 CancelledError，直到外部主动叫停（模拟扛住取消的任务）。"""
    while not stop.is_set():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass


async def _drain(tasks: list[asyncio.Task], stop: asyncio.Event) -> None:
    """测试收尾：让泄漏的重发取消不会拖垮后续用例。"""
    stop.set()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_empty_returns_immediately():
    assert await cancel_tasks([]) == []


@pytest.mark.asyncio
async def test_parked_tasks_die_within_slices():
    stop = asyncio.Event()
    tasks = [asyncio.create_task(_parked(stop), name=f"p{i}") for i in range(3)]
    await asyncio.sleep(0)  # 让任务真正挂到可取消点上
    start = time.monotonic()
    survivors = await cancel_tasks(tasks, timeout=1.0)
    elapsed = time.monotonic() - start
    assert survivors == []
    assert all(t.done() for t in tasks)
    # 快路径不该为宽限期买单（历史回归：sleep 式假界每次 shutdown 恒付 1s）
    assert elapsed < 0.5
    await _drain(tasks, stop)


@pytest.mark.asyncio
async def test_cancel_absorbing_task_abandoned_at_deadline():
    stop = asyncio.Event()
    task = asyncio.create_task(_stubborn(stop), name="stubborn")
    await asyncio.sleep(0)
    start = time.monotonic()
    survivors = await cancel_tasks([task], timeout=0.3)
    elapsed = time.monotonic() - start
    assert survivors == [task]
    # 真实上界：宽到能吸收慢 CI（xdist 争抢）的调度停顿，但绝不允许无界
    # ——若上界丢失（等 stubborn 自然结束），本用例会直接挂到 pytest 超时。
    assert elapsed < 1.0, elapsed
    await _drain([task], stop)


@pytest.mark.asyncio
async def test_mixed_returns_only_survivors():
    stop = asyncio.Event()
    doomed = asyncio.create_task(_parked(stop), name="doomed")
    tough = asyncio.create_task(_stubborn(stop), name="tough")
    await asyncio.sleep(0)
    survivors = await cancel_tasks([doomed, tough], timeout=0.2)
    assert survivors == [tough]
    assert doomed.done() and not tough.done()
    await _drain([doomed, tough], stop)
