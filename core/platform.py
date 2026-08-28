"""PlatformBotResolver —— 平台 bot 解析与多账号登记（加固 main.py 单次探测）。

痛点（架构评审）：bot 晚于插件就绪时，单次探测失败即放弃；'aiocqhttp' 字符串
散落主入口；不可单测。本模块把探测逻辑收敛为可重试、可单测的服务组件。

行为与 main.py _resolve_platform_bot 三级探测完全一致：
  1. context.get_platform(PlatformAdapterType.AIOCQHTTP)
  2. context.get_platform("aiocqhttp")（兼容字符串形态）
  3. context.platform_manager.platform_insts 反射（多账号）
"""

from __future__ import annotations

import asyncio

from core.log import logger


class PlatformBotResolver:
    def __init__(self, context, config=None):
        self._context = context
        self._config = config
        self.bots: list = []  # 全部 OneBot bot（去重，登记顺序）
        self.preferred_bot = None  # 平台适配器 bot（首选后台账号）
        self.last_bot = None  # 最近活跃事件 bot
        # 直接追踪在线的 account_id 集合（不依赖 bot 对象的内存地址）
        self._online_account_ids: set[str] = set()

    # ---------- 探测 ----------

    async def resolve_once(self) -> bool:
        """执行一轮三级探测；返回本轮是否新发现 bot（异常逐级吞掉）。

        新发现的 bot 会立即尝试注册 account_id（通过 get_login_info），
        确保离线清理时能正确识别账号归属。
        """
        found = False
        adapter = self._get_platform_adapter()
        bot = getattr(adapter, "bot", None)
        if bot is not None:
            is_new = self._add_bot(bot)
            found |= is_new
            if self.preferred_bot is None:
                self.preferred_bot = bot
                logger.info(
                    "[group_cloud_storage] platform bot resolved (auto scan ready)"
                )
            # 新 bot 立即注册 account_id（不等待扫描）
            if is_new:
                await self._try_register_account(bot)
        for bot in self._iter_platform_insts():
            if self._add_bot(bot):
                found = True
                logger.info(
                    f"[group_cloud_storage] additional bot resolved "
                    f"(total {len(self.bots)})"
                )
                # 新 bot 立即注册 account_id
                await self._try_register_account(bot)
        return found

    async def _try_register_account(self, bot) -> None:
        """尝试注册 bot 的 account_id（轻量调用，失败静默忽略）。"""
        try:
            info = await asyncio.wait_for(
                bot.call_action("get_login_info"), timeout=5.0
            )
            if info and info.get("user_id"):
                account_id = str(info["user_id"])
                # 直接添加到在线账号集合
                self._online_account_ids.add(account_id)
                logger.info(
                    f"[group_cloud_storage] bot account registered: "
                    f"{account_id} (early detection)"
                )
        except Exception:
            pass  # 静默失败，扫描时会重试

    def _get_platform_adapter(self):
        try:
            from astrbot.api.event import filter  # 延迟 import，可脱离宿主单测

            return self._context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        except Exception:
            pass
        try:
            return self._context.get_platform("aiocqhttp")
        except Exception:
            pass
        return None

    def _iter_platform_insts(self):
        manager = getattr(self._context, "platform_manager", None)
        try:
            for inst in getattr(manager, "platform_insts", None) or []:
                bot = getattr(inst, "bot", None)
                if bot is not None:
                    yield bot
        except Exception:
            return

    def _add_bot(self, bot) -> bool:
        if bot in self.bots:
            return False
        self.bots.append(bot)
        return True

    # ---------- 事件登记 / 选取 ----------

    def register_bot(self, bot) -> None:
        """登记事件 bot（去重入列，并记为最近活跃 bot）。"""
        if bot is None:
            return
        self._add_bot(bot)
        self.last_bot = bot

    def best_bot(self):
        """后台任务选 bot 顺序：最近事件 bot → 平台适配器 bot → 首个登记 bot。"""
        return (
            self.last_bot or self.preferred_bot or (self.bots[0] if self.bots else None)
        )

    # ---------- 账号追踪 ----------

    def register_account(self, account_id: str) -> None:
        """登记在线账号（直接添加到集合）。"""
        if account_id:
            self._online_account_ids.add(str(account_id))

    def unregister_account(self, account_id: str) -> None:
        """取消登记账号（从集合中移除）。"""
        self._online_account_ids.discard(str(account_id))

    def get_online_account_ids(self) -> set[str]:
        """返回当前在线的 account_id 集合（副本）。"""
        return set(self._online_account_ids)

    # ---------- 存活检测 ----------

    async def check_bot_alive(self, bot) -> bool:
        """检测 bot 是否仍然在线（轻量 API 调用，5 秒超时）。"""
        try:
            result = await asyncio.wait_for(
                bot.call_action("get_login_info"), timeout=5.0
            )
            return bool(result and result.get("user_id"))
        except Exception:
            return False

    async def purge_stale_bots(self) -> list[str]:
        """检测并清除已离线的 bot，返回离线的 account_id 列表。"""
        stale_account_ids = []
        alive = []
        for bot in self.bots:
            if await self.check_bot_alive(bot):
                alive.append(bot)
            else:
                # 尝试获取该 bot 的 account_id
                try:
                    info = await asyncio.wait_for(
                        bot.call_action("get_login_info"), timeout=3.0
                    )
                    if info and info.get("user_id"):
                        account_id = str(info["user_id"])
                        stale_account_ids.append(account_id)
                        self._online_account_ids.discard(account_id)
                        logger.info(
                            f"[group_cloud_storage] bot offline detected: "
                            f"account={account_id}"
                        )
                except Exception:
                    # 无法获取 account_id，尝试从数据库查找
                    logger.debug(
                        f"[group_cloud_storage] cannot get account_id for stale bot"
                    )
        self.bots = alive
        # 同步清理 preferred/last 引用
        if self.preferred_bot and self.preferred_bot not in self.bots:
            self.preferred_bot = self.bots[0] if self.bots else None
        if self.last_bot and self.last_bot not in self.bots:
            self.last_bot = None
        return stale_account_ids

    # ---------- 后台重试 ----------

    async def ensure(self, interval_sec: float = 30.0, max_attempts: int = 20) -> bool:
        """轮询 resolve_once 直到发现 bot 或次数耗尽；发现后停止并返回 True。"""
        if self.bots:
            return True
        for attempt in range(1, max_attempts + 1):
            if await self.resolve_once():
                return True
            logger.debug(
                f"[group_cloud_storage] platform bot not ready "
                f"(attempt {attempt}/{max_attempts})"
            )
            await asyncio.sleep(interval_sec)
        logger.warning("[group_cloud_storage] platform bot unresolved after retries")
        return False
