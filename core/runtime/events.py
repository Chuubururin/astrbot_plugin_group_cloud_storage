"""Events — event handling: maps group_upload events to application commands.

Event-handling logic lives here to keep main.py small.
"""
from __future__ import annotations

from core.log import logger


async def handle_group_upload_event(
    raw: dict,
    *,
    sync_service,
    queue,
    capacity_refresher,
    auto_index_enabled: bool = True,
) -> bool:
    """Handle a group_upload event: index the uploaded file and refresh capacity.

    Args:
        raw: raw event JSON (notice_type == group_upload)
        sync_service: ResourceSyncService instance
        queue: OpQueue instance
        capacity_refresher: capacity refresh callback
        auto_index_enabled: whether auto indexing is enabled

    Returns:
        True if event was processed successfully
    """
    if not auto_index_enabled:
        return False
    if raw.get("notice_type") != "group_upload":
        return False
    try:
        if await sync_service.index_event(raw):
            await capacity_refresher(str(raw.get("group_id") or ""))
        return True
    except Exception as e:
        logger.warning(f"[group_cloud_storage] event index failed: {e}")
        return False


async def handle_new_bot_discovered(
    evt_bot,
    resolver,
    queue,
) -> None:
    """Handle a newly discovered bot: register it and trigger an incremental scan.

    Args:
        evt_bot: newly discovered bot instance
        resolver: PlatformBotResolver instance
        queue: OpQueue instance
    """
    resolver.register_bot(evt_bot)
    logger.info(
        f"[group_cloud_storage] new bot via event: "
        f"total {len(resolver.bots)}"
    )
    await queue.submit("scan", target="*", payload={"mode": "incremental"})


async def handle_aiocqhttp_event(
    event,
    *,
    config,
    resolver,
    platform_bots: list,
    queue,
    sync_service,
    dispatch,
    maybe_submit_scan,
    bot_scope,
) -> None:
    """Receive all aiocqhttp platform events (including notices); register
    group_upload events into the index.

    Orchestrates the full on_aiocqhttp logic:
    - Check auto_index_upload_event config
    - Get raw message
    - Call maybe_submit_scan
    - Check for new bot and trigger scan
    - Handle group_upload event

    Args:
        event: AstrMessageEvent instance
        config: PluginConfig instance
        resolver: PlatformBotResolver instance
        platform_bots: list of all OneBot bots (mutable, updated in place)
        queue: OpQueue instance
        sync_service: ResourceSyncService instance
        dispatch: OpDispatcher instance
        maybe_submit_scan: lazy scan callback
        bot_scope: bot context manager (async context manager)
    """
    if not config.get("auto_index_upload_event", True):
        return
    raw = getattr(event.message_obj, "raw_message", None)
    if not raw:
        return
    async with bot_scope(event):
        # On event arrival, check for new bots and submit a catch-up scan
        await maybe_submit_scan()
        # Dynamic bot discovery: trigger a scan if the event bot is new
        evt_bot = getattr(event, "bot", None)
        if evt_bot is not None and id(evt_bot) not in {
            id(b) for b in platform_bots
        }:
            resolver.register_bot(evt_bot)
            platform_bots.clear()
            platform_bots.extend(list(resolver.bots))
            logger.info(
                f"[group_cloud_storage] new bot via event: "
                f"total {len(platform_bots)}"
            )
            await queue.submit(
                "scan", target="*", payload={"mode": "incremental"}
            )
        if raw.get("notice_type") == "group_upload":
            try:
                if await sync_service.index_event(raw):
                    # Event-driven incremental capacity refresh (no group info
                    # sync or manual refresh needed)
                    await dispatch.refresh_capacity(
                        str(raw.get("group_id") or "")
                    )
            except Exception as e:
                logger.warning(f"[group_cloud_storage] event index failed: {e}")


async def on_account_resolved(
    account_id: str,
    *,
    resolver,
    store,
) -> None:
    """Callback after a successful scan: register the online account and set
    only that account's groups to managed=1.

    Does not reset all accounts to managed=0 first: with multi-account scans
    running in parallel, that would briefly hide all data from the page.
    Groups of offline accounts are cleaned up by purge_stale_bots in
    _periodic_resolve_and_scan.

    Args:
        account_id: account ID
        resolver: PlatformBotResolver instance
        store: SqliteMetaStore instance
    """
    resolver.register_account(account_id)
    n = await store.restore_account_groups(account_id)
    logger.info(
        f"[group_cloud_storage] account {account_id} online: "
        f"{n} groups managed=1"
    )
