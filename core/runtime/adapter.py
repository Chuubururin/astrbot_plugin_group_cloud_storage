"""AstrBot-independent runtime host adapter."""
from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from bootstrap import build_components
from adapters.limiter.interval import KeyedLimiter
from adapters.limiter.tier import interval_mult
from adapters.onebot.napcat import NapCatApiAdapter
from core.config import PluginConfig
from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.opctx import account_var
from core.platform import PlatformBotResolver
from core.application.queue import OpDispatcher
from webapi import register_page_apis
from .kernel import RuntimeKernel
from .lifecycle import LifecycleManager

_bot_var: ContextVar = ContextVar("onebot_bot", default=None)


class RuntimeAdapter:
    """Owns runtime composition, platform binding, task tracking and lifecycle."""

    def _init_runtime(self, context, config):
        self.config = PluginConfig(config or {})
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "group_cloud_storage"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task] = set()
        self._runtime_kernel = None
        self._last_bot = self._platform_bot = None
        self._platform_bots: list = []
        self._api_limiter = KeyedLimiter(interval=self.config.request_interval)
        self._resolver = PlatformBotResolver(self.context, self.config)
        components = build_components(bind_call_action=self._bind_call_action, run_handler=self._op_handler,
            ready=self._ensure_init, config=self.config, data_dir=self.data_dir,
            on_account_resolved=self._on_account_resolved, get_online_account_ids=self._get_online_account_ids)
        for key, value in components.items(): setattr(self, key, value)
        self._runtime_kernel = RuntimeKernel(self.services)
        required = ("store", "api", "sync", "queue", "scan", "ops", "transfer", "ingest", "dlserver", "gateway", "task_control", "services", "auto_scan_hours")
        missing = [key for key in required if not hasattr(self, key)]
        if missing: raise RuntimeError(f"bootstrap components missing: {missing}")
        self._lifecycle = LifecycleManager(self._runtime_kernel, resolver=self._resolver, store=self.store, queue=self.queue,
            dlserver=self.dlserver, bridge=getattr(self, "bridge", None), openlist_client=getattr(self, "openlist_client", None),
            config=self.config, auto_scan_hours=self.auto_scan_hours, platform_bots_ref=self._platform_bots)
        self._dispatch = OpDispatcher(services=self.services, api=self.api, store=self.store, sync=self.sync, scan=self.scan,
            ingest=self.ingest, transfer=self.transfer, ops=self.ops, queue=self.queue, config=self.config,
            bots_getter=lambda: self._platform_bots, bridge=getattr(self, "bridge", None),
            bot_api_factory=lambda bot, interval: NapCatApiAdapter(
                lambda action, params: bot.call_action(action, **params), interval=interval))
        # Per-group scan chaining: as soon as a group's info is persisted the
        # dispatcher queues that group's file scan (no bulk wait)
        self.scan.on_group_scanned = self._dispatch._on_group_scanned
        # Group open gate: the scan service needs the live online-account set
        # to exclude offline accounts' groups from lists and targeted reads.
        self.scan.set_online_ids_callback(self._get_online_account_ids)
        register_page_apis(self.context, self.services)
        logger.info("[group_cloud_storage] page apis registered (storage)")

    def _create_runtime_task(self, coro, *, name):
        task = asyncio.create_task(coro, name=name); self._tasks.add(task); task.add_done_callback(self._tasks.discard)
        if self._runtime_kernel: self._runtime_kernel.track_task(task)
        return task

    async def _ensure_init(self):
        await self._lifecycle.init()
        self._platform_bots[:] = self._lifecycle._platform_bots

    async def _bind_call_action(self, action, params):
        # Priority: event bot (chat context) > account-scoped bot (group ops
        # routed to the account that owns the group) > best_bot fallback.
        bot = (
            _bot_var.get()
            or self._resolver.bot_for_account(account_var.get())
            or self._resolver.best_bot()
        )
        if bot is None: raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, action, "no onebot bot in current context")
        await self._api_limiter.acquire(mult=interval_mult(action), account=str(id(bot)))
        return await bot.call_action(action, **params)

    @contextlib.asynccontextmanager
    async def _bot_scope(self, event):
        bot = getattr(event, "bot", None)
        if bot is not None: self._last_bot = bot; self._resolver.register_bot(bot)
        token = _bot_var.set(bot)
        try: yield
        finally: _bot_var.reset(token)

    def _get_online_account_ids(self): return self._resolver.get_online_account_ids()

    async def _on_account_resolved(self, bot, account_id):
        self._resolver.register_account(account_id)
        self._resolver.bind_account_bot(account_id, bot)
        n = await self.store.restore_account_groups(account_id)
        logger.info(f"[group_cloud_storage] account {account_id} online: {n} groups managed=1")
        # Account was actually offline (rows flipped hidden->managed): force a
        # full rescan of its groups so stale capacity/album/essence caches are
        # never reused across an offline period
        if n:
            try:
                gids = await self.store.list_account_group_ids(account_id)
                if gids:
                    await self.queue.submit(
                        "scan", target="*", payload={"mode": "full", "group_ids": gids}
                    )
                    logger.info(f"[group_cloud_storage] account {account_id} back online: full rescan queued for {len(gids)} group(s)")
            except Exception as e:
                logger.debug(f"[group_cloud_storage] post-online rescan queue failed: {e}")

    async def _op_handler(self, op): await self._dispatch.handle(op)
    async def _ensure_ready(self):
        if not self._lifecycle._inited: await self._ensure_init()
    async def terminate(self): await self._lifecycle.terminate()
