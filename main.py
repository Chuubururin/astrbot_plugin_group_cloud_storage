"""群云存储管理器 —— 插件入口（Star 薄壳）。

定位（docs/00）：把 QQ 群三类原生云存储空间（群文件 / 群相册 / 群精华）统一建模为
「群云存储池」。云端为真源；本地仅保存可编码化的元数据索引（SQLite schema v10）。

装配（bootstrap.py）：NapCatApiAdapter（多账号）+ SqliteMetaStore + StorageGateway
（云端/本地/外部三通道）+ 服务依赖注入。
分发（core/services/op_dispatch.py）：OpQueue 12 类操作路由与容量联动。
平台解析（core/platform.py）：多账号 bot 探测/登记/后台重试。
本文件只保留：命令薄壳 + 事件索引 + 生命周期。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from contextvars import ContextVar
from pathlib import Path

# 多文件插件惯例：将插件根目录注入 sys.path，使 `adapters/core/ports/commands`
# 在 AstrBot 以 data.plugins.xxx.main 方式加载时可解析（社区标准做法）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from bootstrap import build_components
from commands.handlers import (
    Services,
    handle_csarchive,
    handle_csbridge,
    handle_csfetch,
    handle_cssave,
    handle_csfile,
    handle_csfiles,
    handle_cssync,
    handle_cshelp,
)
from core.config import PluginConfig
from core.domain.enums import OneBotApiError, OneBotErrorKind
from adapters.limiter.interval import KeyedLimiter
from core.platform import PlatformBotResolver
from core.services.op_dispatch import OpDispatcher
from webapi import register_page_apis

# 当前事件对应的 OneBot bot（CQHttp），供 NapCatApiAdapter 的 call_action 使用。
_bot_var: ContextVar = ContextVar("onebot_bot", default=None)


class Main(Star):
    """群云存储管理器（v2.0）
    /cssync [群号] 同步群云存储索引+统计概览
    /csfiles [群号] [页码] 文件列表
    /csfile <id> [群号] 文件详情+下载直链
    /cssave [群号] <标题> <正文> 文本保存为群精华
    /csfetch [群号] <URL> [文件名] 外部文件入库
    /cshelp 帮助
    """

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = PluginConfig(config or {})
        self.data_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / "group_cloud_storage"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 运行期状态
        self._tasks: set[asyncio.Task] = set()
        self._init_lock = asyncio.Lock()
        self._inited = False
        self._scan_submitted = False
        self._last_bot = None  # 最近活跃事件 bot（回退）
        self._platform_bot = None  # 首选平台适配器 bot（主动获取，不依赖事件）
        self._platform_bots: list = []  # 全部 OneBot 实例（多账号，v9）
        self._scan_account = None  # 当前扫描账号 bot
        self._auto_scan_task: asyncio.Task | None = None
        self._resolve_task: asyncio.Task | None = None
        self._periodic_resolve_task: asyncio.Task | None = None  # v2.12：定期重解析
        # v2.11：QQ 限速按账号键控（每账号独立节奏，跨账号并发；interval 同 request_interval_ms）
        self._api_limiter = KeyedLimiter(
            interval=float(self.config.get("request_interval_ms", 1000)) / 1000.0
        )

        # 平台 bot 解析（探测/登记/后台重试，见 core/platform.py）
        self._resolver = PlatformBotResolver(self.context, self.config)

        # 适配器 / 服务装配（bootstrap.py）；分发器独立（op_dispatch.py）
        components = build_components(
            bind_call_action=self._bind_call_action,
            run_handler=self._op_handler,
            ready=self._ensure_init,
            config=self.config,
            data_dir=self.data_dir,
            on_account_resolved=self._on_account_resolved,
            get_online_account_ids=self._get_online_account_ids,
        )
        for key, value in components.items():
            setattr(self, key, value)
        # 装配契约：bootstrap 返回键与 Main 消费属性必须一一对应（防静默缺属性）
        missing = [
            k
            for k in (
                "store",
                "api",
                "sync",
                "queue",
                "scan",
                "ops",
                "transfer",
                "ingest",
                "dlserver",
                "gateway",
                "task_control",
                "services",
                "auto_scan_hours",
            )
            if not hasattr(self, k)
        ]
        if missing:
            raise RuntimeError(f"bootstrap components missing: {missing}")
        self._dispatch = OpDispatcher(
            services=self.services,
            api=self.api,
            store=self.store,
            sync=self.sync,
            scan=self.scan,
            ingest=self.ingest,
            transfer=self.transfer,
            ops=self.ops,
            queue=self.queue,
            config=self.config,
            bots_getter=lambda: self._platform_bots,
            bridge=getattr(self, "bridge", None),
        )

        # Page 后端 API（群管理/文件检索/队列/SSE）
        register_page_apis(self.context, self.services)
        logger.info("[group_cloud_storage] page apis registered (storage)")

    # ---------- 初始化（异步） ----------

    async def _ensure_init(self) -> None:
        if self._inited:
            return
        async with self._init_lock:
            if not self._inited:
                await self.store.init()
                await self.store.upsert_resources([])  # 预热连接（幂等空操作）
                # v15：任务台账启动对账（ADR-0005：白名单续传候选，其余置 failed）
                await self.task_control.reconcile()
                await self.queue.start()  # worker 常驻（不依赖扫描/事件路径）
                self._inited = True
                await self.dlserver.start()
                # 主动获取平台 bot（无需事件）：Page/命令首调即可触发扫描
                await self._resolve_platform_bot()
                # 惰性启动扫描（"我创建的群"，docs/09 §12.4 启动挂钩）
                await self._maybe_submit_scan()
                # v2.12：定期重解析（动态发现新 bot + 触发扫描）
                if (
                    self._periodic_resolve_task is None
                    or self._periodic_resolve_task.done()
                ):
                    self._periodic_resolve_task = asyncio.create_task(
                        self._periodic_resolve_and_scan(),
                        name="periodic-bot-resolve",
                    )
                # Bridge recovery (B-class necessary auto, REQ-18)
                bridge = getattr(self, "bridge", None)
                if bridge is not None:
                    asyncio.create_task(bridge.recover(), name="bridge-recover")

    async def _resolve_platform_bot(self) -> None:
        """主动获取 aiocqhttp 平台 bot（探测逻辑收敛到 PlatformBotResolver）。

        探测失败时启动后台重试（反向 WS 重连的 bot 晚于插件就绪的场景），
        发现后自动停止；事件路径（_bot_scope）始终可用作兜底。
        """
        try:
            await self._resolver.resolve_once()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] platform bot resolve failed: {e}")
        self._platform_bot = self._resolver.preferred_bot
        self._platform_bots = list(self._resolver.bots)
        if not self._platform_bots and (
            self._resolve_task is None or self._resolve_task.done()
        ):
            self._resolve_task = asyncio.create_task(
                self._resolver.ensure(interval_sec=30.0, max_attempts=20),
                name="platform-bot-resolve",
            )

    def _get_bots(self) -> list:
        return self._platform_bots

    async def _periodic_resolve_and_scan(self) -> None:
        """v2.12：定期重解析平台 bot（动态发现新账号连接 + 检测离线账号清理数据）。

        每 60 秒重解析一次：
        1. 检测已离线的 bot → 标记其群为 managed=0（数据保留但隐藏）
        2. 发现新 bot → 触发增量扫描
        """
        while True:
            await asyncio.sleep(60)
            try:
                # 1. 检测已离线的 bot 并清理其数据
                stale_account_ids = await self._resolver.purge_stale_bots()
                for account_id in stale_account_ids:
                    n = await self.store.mark_account_groups_managed(account_id, 0)
                    if n:
                        logger.info(
                            f"[group_cloud_storage] account {account_id} "
                            f"offline: {n} groups hidden (managed=0)"
                        )
                self._platform_bots = list(self._resolver.bots)

                # 2. 检测新 bot
                old_bots = set(id(b) for b in self._resolver.bots)
                await self._resolver.resolve_once()
                self._platform_bots = list(self._resolver.bots)
                new_bots = [b for b in self._resolver.bots if id(b) not in old_bots]
                if new_bots:
                    logger.info(
                        f"[group_cloud_storage] dynamic bot discovery: "
                        f"{len(new_bots)} new bot(s), total {len(self._resolver.bots)}"
                    )
                    # 为新 bot 触发扫描
                    await self.queue.submit(
                        "scan", target="*", payload={"mode": "incremental"}
                    )
            except Exception as e:
                logger.debug(f"[group_cloud_storage] periodic resolve failed: {e}")

    async def _on_account_resolved(self, bot, account_id: str) -> None:
        """扫描成功后回调：登记在线账号 + 仅将该账号群设为 managed=1。

        v2.13 修复：不再 mark_all=0 再恢复——多账号并行扫描时，
        mark_all=0 会导致短暂的全量 403 窗口，Page 数据全部不可见。
        改为仅增量设置当前账号群为 managed=1，离线账号的群由
        _periodic_resolve_and_scan 的 purge_stale_bots 负责清理。
        """
        self._resolver.register_account(account_id)
        n = await self.store.restore_account_groups(account_id)
        logger.info(
            f"[group_cloud_storage] account {account_id} online: "
            f"{n} groups managed=1"
        )

    def _get_online_account_ids(self) -> set[str]:
        """返回当前在线的 account_id 集合（基于 resolver 的在线账号追踪）。"""
        return self._resolver.get_online_account_ids()

    async def _maybe_submit_scan(self) -> None:
        """惰性启动增量扫描：平台 bot 或事件 bot 就绪即提交（Page 首调即自动扫描）。"""
        if self._scan_submitted:
            return
        if not self._resolver.bots:
            logger.debug(
                "[group_cloud_storage] initial scan deferred: no bot available"
            )
            return
        self._scan_submitted = True
        await self.queue.start()
        await self.queue.submit("scan", target="*", payload={"mode": "incremental"})
        logger.info("[group_cloud_storage] initial group scan queued")
        # 定时自动重扫（保持数据新鲜；0=关闭）
        if self.auto_scan_hours > 0 and (
            self._auto_scan_task is None or self._auto_scan_task.done()
        ):
            self._auto_scan_task = asyncio.create_task(
                self._auto_scan_loop(), name="auto-scan"
            )

    async def _auto_scan_loop(self) -> None:
        """2026-09-01 D-4/W-8：周期差分对账（增补平衡替代定时全量）。

        每 interval 小时提交一次 diff_file_scan（根+一级文件夹目录级列表，
        云端消失条目即刻凋零剔除；缺席冻结不误删）；全量扫描降级为手动例外
        （files/scan mode=all/range，操作受任务 Tab 管控）。
        """
        while True:
            await asyncio.sleep(self.auto_scan_hours * 3600)
            try:
                await self.queue.submit(
                    "diff_file_scan", target="*", payload={"mode": "diff"}
                )
                logger.info("[group_cloud_storage] periodic diff scan queued")
            except Exception as e:
                logger.warning(f"[group_cloud_storage] diff scan submit failed: {e}")

    # ---------- OpQueue 执行分发（薄壳，路由在 OpDispatcher） ----------

    async def _op_handler(self, op) -> None:
        await self._dispatch.handle(op)

    # ---------- bot 绑定（ContextVar，多账号安全） ----------

    async def _bind_call_action(self, action: str, params: dict):
        # 前台事件优先；后台任务依次回退最近事件 bot / 平台适配器 bot（主动获取）
        bot = _bot_var.get() or self._resolver.best_bot()
        if bot is None:
            raise OneBotApiError(
                OneBotErrorKind.LOCAL_ERROR, action, "no onebot bot in current context"
            )
        # v2.11：限速面向每个账号并发作用（多账号互不阻塞）
        await self._api_limiter.acquire(key=str(id(bot)))
        return await bot.call_action(action, **params)

    @contextlib.asynccontextmanager
    async def _bot_scope(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._last_bot = bot  # 供后台任务复用
            self._resolver.register_bot(bot)
        token = _bot_var.set(bot)
        try:
            yield
        finally:
            _bot_var.reset(token)

    # ---------- 命令薄壳（parse → authorize → service） ----------

    @filter.command("cssync")
    async def cssync(self, event: AstrMessageEvent, group_id: str = ""):
        """同步群云存储索引并输出统计概览"""
        if not self._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_cssync(event, self.services, group_id)
            )

    @filter.command("csfiles")
    async def csfiles(self, event: AstrMessageEvent, group_id: str = "", page: int = 1):
        """分页列出群文件"""
        if not self._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csfiles(event, self.services, group_id, int(page))
            )

    @filter.command("csfile")
    async def csfile(self, event: AstrMessageEvent, id: str, group_id: str = ""):
        """文件详情与下载直链"""
        if not self._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            try:
                fid = int(id)
            except (TypeError, ValueError):
                yield event.plain_result(
                    "❌ 参数错误：ID 必须为数字（/csfiles 列表中的编号）。"
                )
                return
            yield event.plain_result(
                await handle_csfile(event, self.services, fid, group_id)
            )

    @filter.command("csarchive")
    async def csarchive(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
        file_ref: str = "",
        force: str = "",
    ):
        """Archive group file to OpenList"""
        if not self._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csarchive(
                    event,
                    self.services,
                    group_id,
                    file_ref,
                    force="--force" in force or force.lower() == "true",
                )
            )

    @filter.command("csbridge")
    async def csbridge(
        self, event: AstrMessageEvent, action: str = "", task_id: str = ""
    ):
        """Bridge task management"""
        if not self._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csbridge(event, self.services, action, task_id)
            )

    @filter.command("cshelp")
    async def cshelp(self, event: AstrMessageEvent):
        """显示指令帮助"""
        yield event.plain_result(handle_cshelp())

    @filter.command("cssave")
    async def cssave(self, event: AstrMessageEvent):
        """文本保存为群精华（长文本自动分段）"""
        if not self._inited:
            await self._ensure_init()
        rest = _strip_command(event)
        group_id, title, text = "", "", ""
        tokens = rest.split(" ", 2)
        if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
            group_id = tokens.pop(0)
        if tokens:
            title = tokens.pop(0)
        if tokens:
            text = tokens[0]
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_cssave(event, self.services, group_id, title, text)
            )

    @filter.command("csfetch")
    async def csfetch(self, event: AstrMessageEvent):
        """拉取外部文件存入群文件"""
        if not self._inited:
            await self._ensure_init()
        rest = _strip_command(event)
        group_id, url, name = "", "", ""
        tokens = rest.split(" ", 2)
        if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
            group_id = tokens.pop(0)
        if tokens:
            url = tokens.pop(0)
        if tokens:
            name = tokens[0]
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csfetch(event, self.services, group_id, url, name)
            )

    # ---------- 事件索引（group_upload，M4 / AC3） ----------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_aiocqhttp(self, event: AstrMessageEvent):
        """接收 aiocqhttp 平台全部事件（含 notice），识别 group_upload 后入库。"""
        if not self.config.get("auto_index_upload_event", True):
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if not raw:
            return
        async with self._bot_scope(event):
            # v2.12：事件触发时检查新 bot 并补发扫描
            await self._maybe_submit_scan()
            # 动态发现：事件 bot 若为新 bot，触发扫描
            evt_bot = getattr(event, "bot", None)
            if evt_bot is not None and id(evt_bot) not in {
                id(b) for b in self._platform_bots
            }:
                self._resolver.register_bot(evt_bot)
                self._platform_bots = list(self._resolver.bots)
                logger.info(
                    f"[group_cloud_storage] new bot via event: "
                    f"total {len(self._platform_bots)}"
                )
                await self.queue.submit(
                    "scan", target="*", payload={"mode": "incremental"}
                )
            if raw.get("notice_type") == "group_upload":
                try:
                    if await self.sync.index_event(raw):
                        # 事件驱动容量增量更新（无需群信息同步/手动刷新）
                        await self._dispatch.refresh_capacity(
                            str(raw.get("group_id") or "")
                        )
                except Exception as e:
                    logger.warning(f"[group_cloud_storage] event index failed: {e}")

    # ---------- 生命周期 ----------

    async def terminate(self):
        """插件禁用/重载时调用：清理自动扫描/OpQueue/SQLite（docs/06 §4 清理约定）。"""
        if self._resolve_task:
            self._resolve_task.cancel()
        if self._periodic_resolve_task:
            self._periodic_resolve_task.cancel()
        try:
            await self.dlserver.shutdown()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] dlserver shutdown failed: {e}")
        if self._auto_scan_task:
            self._auto_scan_task.cancel()
        # Bridge cleanup (REQ-18)
        bridge = getattr(self, "bridge", None)
        if bridge is not None:
            try:
                await bridge.stop_polling()
            except Exception as e:
                logger.warning(f"[group_cloud_storage] bridge stop failed: {e}")
        openlist_client = getattr(self, "openlist_client", None)
        if openlist_client is not None:
            try:
                await openlist_client.aclose()
            except Exception as e:
                logger.warning(
                    f"[group_cloud_storage] openlist client close failed: {e}"
                )
        try:
            await self.queue.shutdown()
        except Exception as e:
            logger.warning(f"[group_cloud_storage] queue shutdown failed: {e}")
        try:
            await self.store.close()
        except Exception as e:  # 生命周期清理失败不影响主流程
            logger.warning(f"[group_cloud_storage] close store failed: {e}")
        logger.info("OneBot Resource Manager terminated.")


def _strip_command(event: AstrMessageEvent) -> str:
    """提取命令参数部分（去掉 /命令 前缀；兼容 @机器人 前缀）。"""
    msg = (getattr(event, "message_str", "") or "").strip()
    if not msg:
        return ""
    tokens = msg.split(None, 1)
    if len(tokens) < 2:
        return ""
    return tokens[1].strip()
