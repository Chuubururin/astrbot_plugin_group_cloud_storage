"""OpDispatcher —— OpQueue 操作分发（自 main.py 拆出，M0 工程加固）。

Main 只保留薄壳 _op_handler 委托；scan/file_scan/sync/文件操作/精华/传输/批量
等 12 类分发与容量联动、扫描进度节流全部收敛于此，可脱离 Star 宿主独立测试。

v2.12：多账号并行扫描 —— 每 bot 独立 adapter + 信号量控制并发。
v2.13：可配置硬顶 + 随机抖动 + 账号级熔断器 + 群哈希分片去重。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from adapters.limiter.interval import IntervalLimiter
from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger


@dataclass
class BotHealth:
    """账号级健康状态（熔断器核心）。"""
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    cooldown_until: float = 0.0  # monotonic time; now < cooldown_until → 暂停

    @property
    def is_cooling(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0  # 成功即解除冷却
        self.total_successes += 1

    def record_failure(self, cooldown_seconds: float = 30.0) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        # ≥3 次连续失败 → 进入冷却
        if self.consecutive_failures >= 3:
            self.cooldown_until = time.monotonic() + cooldown_seconds
            logger.warning(
                f"[circuit-breaker] bot entering cooldown "
                f"{cooldown_seconds}s (consecutive={self.consecutive_failures})"
            )


class OpDispatcher:
    def __init__(
        self,
        services: object,
        api: object,
        store: object,
        sync: object,
        scan: object,
        ingest: object | None,
        transfer: object | None,
        ops: object,
        queue: object,
        config: dict,
        bots_getter: Callable[..., Any],
        bridge: object | None = None,
    ):
        self.services = services
        self.api = api
        self.store = store
        self.sync = sync
        self.scan = scan
        self.ingest = ingest
        self.transfer = transfer
        self.ops = ops
        self.queue = queue
        self.config = config
        self.bridge = bridge
        self._bots_getter = bots_getter
        # 并行扫描信号量（可配置硬顶，默认 8，防风控叠加）
        max_concurrent = int(config.get("max_concurrent_scans", 8))
        max_concurrent = max(1, min(max_concurrent, 20))
        self._scan_semaphore = asyncio.Semaphore(max_concurrent)
        # 账号级熔断器（每 bot 独立健康分）
        self._bot_health: dict[str, BotHealth] = {}
        # 全局熔断：过半账号同时冷却 → 全局暂停 10 分钟
        self._global_cooldown_until: float = 0.0
        logger.info(f"[op-queue] scan concurrency: {max_concurrent}")

    # ── 群哈希分片 ──────────────────────────────────────────────
    def _assign_groups(
        self, all_groups: list[dict], bots: list
    ) -> dict[int, list[str]]:
        """将群列表按 hash(group_id) % len(bots) 稳定分片到各账号。

        返回 {bot_index: [group_id, ...]}。账号下线时其分片自然迁移
        （hash 重算，无需显式迁移表）。
        """
        if not bots:
            return {}
        n = len(bots)
        assignment: dict[int, list[str]] = {i: [] for i in range(n)}
        for g in all_groups:
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            shard = int(hashlib.md5(gid.encode()).hexdigest(), 16) % n
            assignment[shard].append(gid)
        return assignment

    def _bot_id(self, bot) -> str:
        """提取 bot 稳定标识（用于 health map key）。"""
        return str(getattr(bot, "_uin", None) or id(bot))

    def _check_global_circuit_breaker(self, bots: list) -> bool:
        """检查全局熔断：过半账号同时冷却 → 返回 True（应暂停全部）。"""
        now = time.monotonic()
        if now < self._global_cooldown_until:
            return True
        if not bots:
            return False
        cooling_count = sum(
            1 for b in bots
            if self._bot_health.get(self._bot_id(b), BotHealth()).is_cooling
        )
        if cooling_count > len(bots) / 2:
            pause = 600  # 10 分钟
            self._global_cooldown_until = now + pause
            logger.warning(
                f"[circuit-breaker] GLOBAL PAUSE {pause}s — "
                f"{cooling_count}/{len(bots)} bots in cooldown (IP-level throttle?)"
            )
            return True
        return False

    async def _run_scan_for_bot(
        self, bot, mode: str, group_filter: list[str] | None = None
    ) -> None:
        """为单个 bot 创建独立 adapter 执行扫描（无全局状态竞争）。"""
        from adapters.onebot.napcat import NapCatApiAdapter

        bot_id = self._bot_id(bot)
        health = self._bot_health.setdefault(bot_id, BotHealth())

        # 熔断检查：该账号在冷却中则跳过
        if health.is_cooling:
            logger.debug(f"[circuit-breaker] skipping bot {bot_id} (cooling)")
            return

        try:
            interval = float(self.config.get("request_interval_ms", 1000)) / 1000.0
        except (TypeError, ValueError):
            interval = 1.0
        # ±20% 随机抖动，防多账号同相位触发服务端聚合风控
        jitter = interval * random.uniform(0.8, 1.2)
        bot_api = NapCatApiAdapter(
            lambda action, params: bot.call_action(action, **params),
            interval=jitter,
        )
        async with self._scan_semaphore:
            try:
                if mode == "incremental":
                    result = await self.scan.scan_owned_incremental(
                        account_bot=bot, api_override=bot_api,
                        group_filter=group_filter,
                    )
                else:
                    result = await self.scan.scan_owned(
                        account_bot=bot, api_override=bot_api,
                        group_filter=group_filter,
                    )
                # 熔断信号：失败占比 >50% 视为该 bot 本次失败
                total = getattr(result, "total", 0) or 0
                failed = getattr(result, "failed", 0) or 0
                if total > 0 and failed > total / 2:
                    health.record_failure()
                    logger.warning(
                        f"[circuit-breaker] bot {bot_id}: "
                        f"{failed}/{total} groups failed (>50%)"
                    )
                else:
                    health.record_success()
            except Exception as e:
                health.record_failure()
                logger.warning(
                    f"[op-queue] scan bot {bot_id} failed: {e}"
                )

    async def handle(self, op) -> None:
        if op.kind == "scan":
            bots = self._bots_getter() or [None]
            mode = op.payload.get("mode")
            if len(bots) <= 1:
                # 单 bot 或无 bot：保持原有路径（零开销）
                b = bots[0] if bots else None
                if mode == "incremental":
                    await self.scan.scan_owned_incremental(account_bot=b)
                else:
                    await self.scan.scan_owned(account_bot=b)
            else:
                # 全局熔断检查
                if self._check_global_circuit_breaker(bots):
                    logger.warning("[op-queue] scan skipped: global circuit breaker")
                    return
                # 稳定分片：按 bot id 排序，消除 getiter 顺序抖动
                bots_sorted = sorted(bots, key=lambda b: self._bot_id(b))
                # 群哈希分片：从各 bot API 拉群列表取并集（发现新群的关键路径）
                all_group_ids: set[str] = set()
                for b in bots_sorted:
                    try:
                        from adapters.onebot.napcat import NapCatApiAdapter
                        _api = NapCatApiAdapter(
                            lambda action, params, _b=b: _b.call_action(action, **params),
                            interval=0.1,
                        )
                        raw = await _api.list_groups()
                        for g in raw:
                            gid = str(g.get("group_id") or "")
                            if gid:
                                all_group_ids.add(gid)
                    except Exception as e:
                        logger.debug(f"[op-queue] pre-scan list_groups failed for {self._bot_id(b)}: {e}")
                all_groups = [{"group_id": gid} for gid in all_group_ids]
                assignment = self._assign_groups(all_groups, bots_sorted)
                # 多 bot 并行（每 bot 独立 adapter + 分片群列表）
                tasks = [
                    asyncio.create_task(
                        self._run_scan_for_bot(b, mode, assignment.get(i, [])),
                        name=f"scan-{self._bot_id(b)}",
                    )
                    for i, b in enumerate(bots_sorted)
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        elif op.kind == "file_scan":
            await self.do_file_scan(op)
        elif op.kind == "diff_file_scan":
            # 2026-09-01 D-4/W-8：凋零差分对账（根+一级文件夹列表；周期调度，
            # 替代定时全量；缺席冻结不凋零；全量仍为手动例外）
            await self.do_diff_scan(op)
        elif op.kind == "rename":
            await self.scan.rename_remote(
                op.target,
                op.payload["name"],
                display_name=op.payload.get("display_name"),
                label=op.payload.get("label"),
            )
        elif op.kind == "sync_all":
            raise ValueError("sync_all removed: use files/scan (all/range)")
        elif op.kind == "sync":
            lock = self.services.lock_for(op.target)
            result = await self.sync.run_full_sync(op.target, lock)
            await self.refresh_capacity(op.target)  # 文件同步后联动容量统计
            if not result.ok and result.error:
                raise RuntimeError(result.error)
        elif op.kind in (
            "upload",
            "delete",
            "move_file",
            "replace_name",
            "convert_volumes",
        ):
            await self.run_file_op_and_announce(op)
        elif op.kind == "create_folder":
            await self.ops.handle(op)
        elif op.kind in (
            "essence_save",
            "essence_delete",
            "fetch",
            "video_upload",
            "video_album",
            "image_album",  # 2026-09-01 N-06：单图导入群相册
        ):
            await self.ingest.handle(op)
        elif op.kind == "netdisk_index":
            # N4 深度索引（ADR-0004）：手动任务，目录粒度限速可取消
            if self.services.netdisk is None:
                raise OneBotApiError(
                    OneBotErrorKind.LOCAL_ERROR,
                    op.kind,
                    "netdisk service not configured",
                )
            await self.services.netdisk.handle_index(op)
        elif op.kind in ("bridge_out", "bridge_in"):
            if self.bridge is None:
                raise OneBotApiError(
                    OneBotErrorKind.LOCAL_ERROR,
                    op.kind,
                    "bridge service not configured",
                )
            if op.kind == "bridge_out":
                await self.bridge.handle_bridge_out(op)
            else:
                await self.bridge.handle_bridge_in(op)
        elif op.kind == "batch_groups":
            await self.scan.run_batch_ops(op)
        else:
            # 未知 kind 属程序性错误：LOCAL_ERROR 不重试（重试无法修复）
            raise OneBotApiError(
                OneBotErrorKind.LOCAL_ERROR, op.kind, f"unknown op kind: {op.kind}"
            )

    async def run_file_op_and_announce(self, op) -> None:
        """文件操作执行后：容量增量自动写库 + KV 维护 + data_changed 推送。"""
        try:
            await self.ops.handle(op)
            # 删除/改名重传后：整群文件列表校验刷新（mark_missing_as_deleted），
            # 确保本地显示与云端严格一致（防止过期索引行残留 active）
            if op.kind in ("delete", "replace_name"):
                lock = self.services.lock_for(op.target)
                await self.sync.run_full_sync(op.target, lock)
            await self.refresh_capacity(op.target)  # 增量容量：操作完成即算即写
        finally:
            if self.services.searchkv is not None and op.kind in (
                "upload",
                "delete",
                "move_file",
            ):
                self.services.searchkv.mark_dirty(op.target)
            self.queue.publish(
                {
                    "type": "data_changed",
                    "kind": op.kind,
                    "target": op.target,
                    "ts": time.time(),
                }
            )

    async def do_file_scan(self, op) -> None:
        """群内文件扫描（全量/范围）：逐群 full_sync + 容量联动；进度实时发布。"""
        if op.payload.get("mode") == "all":
            groups = await self.scan.list_page_groups(
                self.config.get("managed_groups", [])
            )
            targets = [g.group_id for g in groups]
        else:
            targets = op.payload.get("groups") or []
        total = len(targets)
        failed = 0
        last_fail: str | None = None
        consecutive = 0
        last_pub = 0.0
        last_log = 0.0
        for i, gid in enumerate(targets, 1):
            # v15：协作式检查点——中断（取消）与暂停在群间生效（D-6 定时扫描受管控）
            await self.queue.pause_check(op)
            # §18.2 加严：逐群调用前置限速（3×1000ms=3s/次调用）
            await self.queue.acquire(mult=3.0)
            lock = self.services.lock_for(gid)
            result = await self.sync.run_full_sync(gid, lock)
            await self.refresh_capacity(gid)
            now = time.monotonic()
            if not result.ok and result.error:
                failed += 1
                consecutive += 1
                last_fail = last_fail or str(result.error)
                # §18.3：退避/远端批量失败降 debug，每 20 群汇总一次 warning
                logger.debug(f"[file-scan] {gid} failed: {result.error}")
                if (failed % 20 == 0) or (now - last_log > 30):
                    logger.warning(
                        f"[file-scan] {failed}/{i} groups failed so far "
                        f"(e.g. {last_fail[:80]})"
                    )
                    last_log = now
                # 风控冷却：连续失败 → 冷却 60s 再续（QQ 限流恢复窗口）
                if consecutive >= 10:
                    logger.warning(
                        f"[file-scan] {consecutive} consecutive failures; "
                        f"cooling 60s (risk control)"
                    )
                    await asyncio.sleep(60)
                    consecutive = 0
                    last_log = now
            else:
                consecutive = 0
            # §18.3：进度 publish 节流（≥2s 或每 10 群）
            if (now - last_pub >= 2.0) or (i % 10 == 0):
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "file_scan",
                        "target": gid,
                        "i": i,
                        "n": total,
                        "detail": f"群 {gid}",
                    }
                )
                # 边扫边刷新：文件列表与容量随扫描进度即时可见
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "file_scan",
                        "target": "*" if op.payload.get("mode") == "all" else gid,
                        "i": i,
                        "n": total,
                    }
                )
                last_pub = now
        # 扫描完成：受影响群索引失效（懒重建）+ 动态刷新事件
        if self.services.searchkv is not None:
            for gid in targets:
                self.services.searchkv.mark_dirty(gid)
        self.queue.publish(
            {
                "type": "data_changed",
                "kind": "file_scan",
                "target": "*",
                "ts": time.time(),
            }
        )
        logger.info(
            f"[file-scan] done: {total} groups (mode={op.payload.get('mode')}, "
            f"failed={failed})"
        )

    async def do_diff_scan(self, op) -> None:
        """2026-09-01 D-4/W-8：凋零差分对账（周期调度替代定时全量）。

        - 目标：全部受管群（target="*"）或给定群（op.target）；
        - 每群执行 run_diff_sync（根+一级文件夹列表，目录级）；
        - 缺席（complete=False）→ 冻结该群（不凋零）；连续失败冷却 60s；
        - 协作式检查点（pause_check）保证定时扫描受任务 Tab 管控（D-6）；
        - 全量扫描不在此路径（files/scan all 仍为手动例外）。
        """
        if op.target and op.target != "*":
            targets = [str(op.target)]
        else:
            groups = await self.scan.list_page_groups(
                self.config.get("managed_groups", [])
            )
            targets = [g.group_id for g in groups]
        total = len(targets)
        failed = 0
        withered = 0
        consecutive = 0
        last_fail: str | None = None
        last_pub = 0.0
        for i, gid in enumerate(targets, 1):
            await self.queue.pause_check(op)
            await self.queue.acquire(mult=3.0)
            lock = self.services.lock_for(gid)
            result = await self.sync.run_diff_sync(gid, lock)
            now = time.monotonic()
            if not result.ok:
                failed += 1
                consecutive += 1
                last_fail = last_fail or str(result.error)
                if consecutive >= 10:
                    logger.warning(
                        f"[diff-scan] {consecutive} consecutive frozen groups; "
                        f"cooling 60s (risk control)"
                    )
                    await asyncio.sleep(60)
                    consecutive = 0
            else:
                consecutive = 0
                withered += result.files_removed
                # 差分对账成功后联动容量（索引兜底口径，轻量）
                try:
                    await self.refresh_capacity(gid)
                except Exception as e:
                    logger.debug(f"[diff-scan] capacity refresh failed for {gid}: {e}")
            if (now - last_pub >= 2.0) or (i % 10 == 0) or i == total:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "diff_file_scan",
                        "target": gid,
                        "i": i,
                        "n": total,
                        "detail": f"差分 {gid}",
                    }
                )
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "diff_file_scan",
                        "target": "*",
                        "i": i,
                        "n": total,
                    }
                )
                last_pub = now
        if self.services.searchkv is not None:
            for gid in targets:
                self.services.searchkv.mark_dirty(gid)
        self.queue.publish(
            {
                "type": "data_changed",
                "kind": "diff_file_scan",
                "target": "*",
                "ts": time.time(),
            }
        )
        logger.info(
            f"[diff-scan] done: {total} groups (failed={failed}, withered={withered})"
        )

    async def refresh_capacity(self, group_id: str) -> None:
        """容量统一口径并持久化到 groups 表。

        fs API 失败时返回 None → 跳过写入，保留上次真值，杜绝 0 值覆盖震荡。
        """
        try:
            await self.queue.acquire()
            result = await self.capacity_of(group_id)
            if result is None:
                return
            used, total, count, limit = result
            await self.store.update_group_fields(
                group_id,
                used_space=used,
                total_space=total,
                file_count=count,
                limit_count=limit,
            )
        except Exception as e:
            logger.debug(f"[group-scan] capacity refresh failed for {group_id}: {e}")

    async def capacity_of(self, group_id: str) -> tuple[int, int, int, int] | None:
        """统一容量口径。

        fs 成功 → 返回四元组（字段级本地索引兜底 used/count）。
        fs 失败或返回 total=0 → 返回 None（调用方跳过写入，保留上次值）。
        """
        try:
            fs = await self.api.get_group_fs_info(group_id)
        except Exception:
            return None
        if not fs.total_space:
            return None
        used = fs.used_space or await self.store.sum_resource_sizes(group_id)
        count = fs.file_count or await self.store.count_active(group_id)
        return used, fs.total_space, count, fs.limit_count
