"""Bootstrap — service assembly factory.

The Star entry point keeps only command shells, events and lifecycle; all
service assembly lives here, and together with OpDispatcher the entry stays
thin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger

from adapters.limiter.interval import KeyedLimiter
from adapters.onebot.napcat import NapCatApiAdapter
from adapters.persistence.sqlite import SqliteMetaStore
from commands.handlers import Services
from core.config import PluginConfig
from core.application.policies import PermissionService
from core.application.files import FileOpsService
from core.application.ingest import CloudIngestService
from core.application.files.converter import ConverterService
from core.application.catalog import (
    ResourceQueryService, StatsService, SearchKV, StoragePlanner,
)
from core.application.transfer import TransferService, download_endpoint_origins
from core.application.distributor import DistributorService
from core.application.sync import ResourceSyncService, GroupScanService
from core.application.queue import OpQueue, TaskControlService
from core.application.bridge import BridgeService
from core.application.netdisk import NetdiskService
from core.application.download_server import DownloadServerService
from core.application.database import DatabaseAdminService
from adapters.external.openlist import OpenListClient


def build_components(
    bind_call_action,
    run_handler,
    ready,
    config: dict | PluginConfig,
    data_dir: Path,
    on_account_resolved=None,
    get_online_account_ids=None,
) -> dict:
    """Assemble all services and return the components dict (keys match Main's
    attribute names).

    - bind_call_action / run_handler / ready are injected by the host
      (avoids circular dependencies)
    - config is wrapped in PluginConfig: get() passes through with the same
      semantics as the raw dict
    - on_account_resolved: callback after a successful scan
      (bot, account_id) -> register mapping + restore managed flags
    - get_online_account_ids: callback returning the set of online account_ids
    """
    cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
    for key, msg in cfg.validate():
        logger.warning(f"[group_cloud_storage] config warning: {key} {msg}")

    from core.application.files import consts as files_consts
    files_consts.configure(cfg)

    interval = cfg.request_interval

    store = SqliteMetaStore(data_dir / "meta.db")
    database_admin = DatabaseAdminService(
        store, data_dir=data_dir, token=str(cfg.get("database_admin_token", "") or "")
    )
    api = NapCatApiAdapter(bind_call_action, interval=interval)
    perm = PermissionService(
        managed_groups=cfg.get("managed_groups", []),
        global_admin_qqs=cfg.get("global_admin_qqs", []),
    )
    sync = ResourceSyncService(api, store)

    # Group management / page: shared rate limiter (global pacing for the
    # OpQueue and composite scan operations). Keyed per account: the default
    # key carries the global pace; OpQueue consumes it via the RateLimiter port.
    limiter = KeyedLimiter(interval)

    # Task records and control: record hooks ride along with the queue;
    # the queue and compensation executors are attached after assembly.
    task_control = TaskControlService(store=store, queue=None)
    queue = OpQueue(
        run_handler=run_handler,
        interval=0.05,  # ordering/retry/slots only; QQ pacing per account in the adapter
        limiter=limiter,
        high_priority=set(cfg.op_high_priority_kinds) or None,
        slots=4,  # cross-account consumer slots (half high-priority, half normal)
        ledger=task_control,
    )
    task_control.queue = queue
    scan = GroupScanService(
        api,
        store,
        queue,
        auto_label=bool(cfg.get("auto_label", True)),
        on_account_resolved=on_account_resolved,
        group_info_ttl_hours=float(cfg.get("group_info_ttl_hours", 24) or 0),
    )
    auto_scan_hours = float(cfg.get("auto_scan_interval_hours", 6) or 0)
    # One shared lock dict for every sync path (file-op post-sync inside
    # FileOpsService and file_scan/sync ops via Services.lock_for): two
    # dicts let two run_full_sync race on the same group.
    sync_locks: dict[str, asyncio.Lock] = {}
    ops = FileOpsService(
        api, store, queue, sync, tmp_dir=data_dir / "tmp", config=cfg,
        sync_locks=sync_locks,
    )
    task_control.file_ops = ops  # undo compensation executor

    transfer = TransferService(
        store,
        queue,
        tmp_dir=data_dir / "tmp",
        config=cfg,
        download_info=ops.download_info,
        # Cloud-to-cloud distribution fetches our own download-server direct
        # link; that endpoint is allow-listed here so the SSRF gate does not
        # reject it (it stays closed for every other private address).
        trusted_origins=download_endpoint_origins(cfg),
    )
    converter = ConverterService(tmp_dir=data_dir / "tmp")
    ingest = CloudIngestService(
        api,
        store,
        queue,
        sync,
        tmp_dir=data_dir / "tmp",
        config=cfg,
        transfer=transfer,
        converter=converter,
        sync_locks=sync_locks,
    )
    dlserver = DownloadServerService(
        store,
        config=cfg,
        download_info=ops.download_info,
    )

    # OpenList bridge: only build if enabled.
    #
    # Fail-open on purpose: OpenListClient.__init__ runs validate_base_url
    # (SSRF scheme/host/DNS checks) and BridgeService/NetdiskService wire it
    # up.  A rejection there used to propagate out of _init_runtime -> Main.__init__,
    # so AstrBot could not construct the plugin and ALL 8 command handlers plus
    # the aiocqhttp event hook vanished - collateral damage from a netdisk-only
    # misconfiguration.  A broken netdisk must cost the netdisk, nothing else.
    #
    # Leaving all three as None is a state the runtime already handles: the
    # dispatcher answers bridge/netdisk ops with "bridge service not
    # configured" / "netdisk service not configured" (LOCAL_ERROR, the honest
    # answer), the command handlers answer "Bridge service not configured
    # (openlist_enabled=false).", and LifecycleManager.terminate() guards the
    # close with `if self.openlist_client is not None`.  Same stance as
    # lifecycle.init()'s isolated dlserver.start() - log loudly, keep going.
    bridge = None
    netdisk = None
    openlist_client = None
    if cfg.openlist_enabled and cfg.openlist_base_url:
        try:
            openlist_client = OpenListClient(
                base_url=cfg.openlist_base_url,
                username=cfg.openlist_username,
                password=cfg.openlist_password,
                token=cfg.openlist_token,
                timeout=cfg.openlist_timeout_sec,
                allow_private_address=cfg.openlist_allow_private_address,
            )
            bridge = BridgeService(
                client=openlist_client,
                store=store,
                config=cfg,
                queue=queue,
                api=api,
                ingest=ingest,
                dlserver=dlserver,
            )
            netdisk = NetdiskService(
                client=openlist_client,
                store=store,
                config=cfg,
                queue=queue,
            )
        except Exception as e:
            # Config-level rejection OR construction bug - either way the rest
            # of the plugin has no reason to die.  Log at error level with the
            # offending value so the fix is obvious from the log alone.
            logger.error(
                f"[group_cloud_storage] OpenList bridge/netdisk init failed; "
                f"plugin continues without the netdisk/bridge subset: {e} "
                f"(openlist_base_url={cfg.openlist_base_url!r}, "
                f"openlist_allow_private_address="
                f"{cfg.openlist_allow_private_address!r})"
            )
            openlist_client = bridge = netdisk = None

    services = Services(
        permission=perm,
        store=store,
        api=api,
        sync=sync,
        query=ResourceQueryService(store),
        stats=StatsService(store),
        scan=scan,
        ops=ops,
        planner=StoragePlanner(store),
        searchkv=SearchKV(store),
        queue=queue,
        ingest=ingest,
        transfer=transfer,
        dlserver=dlserver,
        bridge=bridge,
        netdisk=netdisk,
        task_control=task_control,
        converter=converter,
        sync_locks=sync_locks,
        distributor=DistributorService(
            store=store,
            api=api,
            ops=ops,
            bridge=bridge,
            ingest=ingest,
            dlserver=dlserver,
            queue=queue,
            tmp_dir=data_dir / "tmp",
        ),
        config=cfg,
        database_admin=database_admin,
        ready=ready,
        get_online_account_ids=get_online_account_ids,
    )

    return {
        "store": store,
        "database_admin": database_admin,
        "api": api,
        "perm": perm,
        "sync": sync,
        "limiter": limiter,
        "queue": queue,
        "scan": scan,
        "ops": ops,
        "transfer": transfer,
        "ingest": ingest,
        "dlserver": dlserver,
        "bridge": bridge,
        "openlist_client": openlist_client,
        "task_control": task_control,
        "services": services,
        "auto_scan_hours": auto_scan_hours,
    }
