"""PlatformBotResolver — platform bot resolution and multi-account registration.

Discovers OneBot bots with retry support, packaged as a service component that
can be unit-tested without the host:

  1. context.get_platform(PlatformAdapterType.AIOCQHTTP)
  2. context.get_platform("aiocqhttp") (string form fallback)
  3. reflection over context.platform_manager.platform_insts (multi-account)
"""

from __future__ import annotations

import asyncio

from core.log import logger


class PlatformBotResolver:
    def __init__(self, context, config=None):
        self._context = context
        self._config = config
        self.bots: list = []  # all OneBot bots (deduplicated, registration order)
        self.preferred_bot = None  # platform adapter bot (preferred background account)
        self.last_bot = None  # most recently active event bot
        # Tracks the set of online account_ids directly (independent of bot
        # object identity)
        self._online_account_ids: set[str] = set()
        # account_id -> bot, liveness-verified bindings used for per-group
        # account routing of file operations (multi-account support)
        self._account_bots: dict[str, object] = {}

    # ---------- Resolution ----------

    async def resolve_once(self) -> bool:
        """Run one three-step resolution pass; return whether a new bot was
        found (exceptions swallowed at each step).

        Newly found bots immediately try to register their account_id (via
        get_login_info) so offline cleanup can attribute groups correctly.
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
            # Register the new bot's account_id immediately (no wait for a scan)
            if is_new:
                await self._try_register_account(bot)
        for bot in self._iter_platform_insts():
            if self._add_bot(bot):
                found = True
                logger.info(
                    f"[group_cloud_storage] additional bot resolved "
                    f"(total {len(self.bots)})"
                )
                # Register the new bot's account_id immediately
                await self._try_register_account(bot)
        return found

    async def _try_register_account(self, bot) -> None:
        """Try to register the bot's account_id (lightweight call, failures ignored)."""
        try:
            info = await asyncio.wait_for(
                bot.call_action("get_login_info"), timeout=5.0
            )
            if info and info.get("user_id"):
                account_id = str(info["user_id"])
                # Add directly to the online account set
                self._online_account_ids.add(account_id)
                self._account_bots[account_id] = bot
                logger.info(
                    f"[group_cloud_storage] bot account registered: "
                    f"{account_id} (early detection)"
                )
        except Exception:
            pass  # fail silently; the scan will retry

    def _get_platform_adapter(self):
        try:
            from astrbot.api.event import filter  # deferred import; testable without host

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

    # ---------- Event registration / selection ----------

    def register_bot(self, bot) -> None:
        """Register an event bot (deduplicated append, recorded as most recent)."""
        if bot is None:
            return
        self._add_bot(bot)
        self.last_bot = bot

    def best_bot(self):
        """Bot pick order: latest event bot, adapter bot, first registered bot."""
        return (
            self.last_bot or self.preferred_bot or (self.bots[0] if self.bots else None)
        )

    # ---------- Account tracking ----------

    def register_account(self, account_id: str) -> None:
        """Register an online account (added directly to the set)."""
        if account_id:
            self._online_account_ids.add(str(account_id))

    def bind_account_bot(self, account_id: str, bot) -> None:
        """Record a liveness-verified bot for an account (per-group account
        routing input; refreshed by scans, registration and liveness checks)."""
        if account_id and bot is not None:
            self._account_bots[str(account_id)] = bot

    def bot_for_account(self, account_id: str):
        """Bot bound to an account, or None (bindings drop with stale bots)."""
        bot = self._account_bots.get(str(account_id or ""))
        return bot if bot is not None and bot in self.bots else None

    def get_online_account_ids(self) -> set[str]:
        """Return a copy of the currently online account_id set."""
        return set(self._online_account_ids)

    # ---------- Liveness checks ----------

    async def check_bot_alive(self, bot) -> bool:
        """Check whether a bot is still online (lightweight API call, 5s timeout)."""
        try:
            result = await asyncio.wait_for(
                bot.call_action("get_login_info"), timeout=5.0
            )
            return bool(result and result.get("user_id"))
        except Exception:
            return False

    async def purge_stale_bots(self) -> tuple[list[str], list[tuple[str, object]]]:
        """Detect and drop offline bots.

        Returns (offline account_ids, alive (account_id, bot) pairs). One
        get_login_info per bot decides liveness AND refreshes the account
        binding; the caller restores groups of verified-alive accounts so a
        transient timeout cannot hide an account's data forever.
        """
        stale_account_ids: list[str] = []
        alive_accounts: list[tuple[str, object]] = []
        alive = []
        for bot in self.bots:
            try:
                info = await asyncio.wait_for(
                    bot.call_action("get_login_info"), timeout=5.0
                )
            except Exception:
                info = None
            if info and info.get("user_id"):
                account_id = str(info["user_id"])
                alive.append(bot)
                alive_accounts.append((account_id, bot))
                self._online_account_ids.add(account_id)
                self._account_bots[account_id] = bot
            else:
                # Try to get this bot's account_id for the offline record
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
                    # account_id could not be fetched for this stale bot
                    logger.debug(
                        "[group_cloud_storage] cannot get account_id for stale bot"
                    )
        self.bots = alive
        # Also clean up the preferred/last references
        if self.preferred_bot and self.preferred_bot not in self.bots:
            self.preferred_bot = self.bots[0] if self.bots else None
        if self.last_bot and self.last_bot not in self.bots:
            self.last_bot = None
        return stale_account_ids, alive_accounts

    # ---------- Background retry ----------

    async def ensure(self, interval_sec: float = 30.0, max_attempts: int = 20) -> bool:
        """Poll resolve_once until a bot is found or attempts run out; stop on success."""
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
