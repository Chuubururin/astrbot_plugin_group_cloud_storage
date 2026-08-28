"""Bootstrap —— 服务装配工厂（自 main.py 拆出，M0 工程加固）。

Star 入口只保留命令壳/事件/生命周期；全部服务装配收敛于此，
配合 OpDispatcher 使入口从「上帝类」退化为薄壳。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

from adapters.limiter.interval import IntervalLimiter
from adapters.onebot.napcat import NapCatApiAdapter
from adapters.store.sqlite import SqliteMetaStore
from commands.handlers import Services
from core.config import PluginConfig
from core.services.bridge import BridgeService
from core.services.netdisk import NetdiskService
from core.services.cloud_ingest import CloudIngestService
from core.services.download_server import DownloadServerService
from core.services.converter import ConverterService  # noqa: E402
from core.services.distributor import DistributorService  # noqa: E402
from core.services.file_ops import FileOpsService
from core.services.gateway import StorageGateway
from core.services.group_scan import GroupScanService
from core.services.op_queue import OpQueue
from core.services.permission import PermissionService
from core.services.resource_query import ResourceQueryService, StatsService
from core.services.resource_sync import ResourceSyncService
from core.services.search_kv import SearchKV
from core.services.storage_planner import StoragePlanner
from core.services.task_control import TaskControlService
from core.services.transfer import TransferService
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
    """装配全部服务，返回组件 dict（键名 = Main 现有属性名）。

    - bind_call_action / run_handler / ready 由宿主注入（避免循环依赖）
    - config 包装为 PluginConfig：get() 透传语义，行为与原 dict 完全一致
    - on_account_resolved: 扫描成功后回调 (bot, account_id) → 注册映射 + 恢复 managed
    - get_online_account_ids: 返回当前在线 account_id 集合的回调
    """
    cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
    for key, msg in cfg.validate():
        logger.warning(f"[group_cloud_storage] config warning: {key} {msg}")

    interval = float(cfg.get("request_interval_ms", 500)) / 1000.0

    store = SqliteMetaStore(data_dir / "meta.db")
    api = NapCatApiAdapter(bind_call_action, interval=interval)
    perm = PermissionService(
        managed_groups=cfg.get("managed_groups", []),
        global_admin_qqs=cfg.get("global_admin_qqs", []),
    )
    sync = ResourceSyncService(api, store)

    # 群管理/Page（docs/09 §12）：共享限速器（OpQueue 与扫描复合操作全局限速）
    limiter = IntervalLimiter(interval)

    # v15：任务台账与控制（D-6）——台账挂钩随队列；队列/补偿执行器在装配后补入
    task_control = TaskControlService(store=store, queue=None)
    queue = OpQueue(
        run_handler=run_handler,
        interval=0.05,  # v2.11：队列仅保序/重试/槽位；QQ 节奏由适配器按账号键控
        limiter=limiter,
        high_priority=set(cfg.op_high_priority_kinds) or None,
        slots=4,  # 跨账号并发消费槽位（高优/常规各半）
        ledger=task_control,
    )
    task_control.queue = queue
    scan = GroupScanService(
        api,
        store,
        queue,
        auto_label=bool(cfg.get("auto_label", True)),
        on_account_resolved=on_account_resolved,
    )
    auto_scan_hours = float(cfg.get("auto_scan_interval_hours", 6) or 0)
    ops = FileOpsService(api, store, queue, sync, tmp_dir=data_dir / "tmp")
    task_control.file_ops = ops  # 撤销补偿执行器

    transfer = TransferService(
        store,
        queue,
        tmp_dir=data_dir / "tmp",
        config=cfg,
        download_info=ops.download_info,
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
    )
    dlserver = DownloadServerService(
        store,
        config=cfg,
        download_info=ops.download_info,
    )
    gateway = StorageGateway(
        cloud=api,
        local=store,
        ingest=ingest,
        transfer=transfer,
        dlserver=dlserver,
        fileops=ops,
    )

    # OpenList bridge (REQ-05/08): only build if enabled
    bridge = None
    netdisk = None
    openlist_client = None
    if cfg.openlist_enabled and cfg.openlist_base_url:
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
        gateway=gateway,
        bridge=bridge,
        netdisk=netdisk,
        task_control=task_control,
        converter=converter,
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
        ready=ready,
        get_online_account_ids=get_online_account_ids,
    )

    return {
        "store": store,
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
        "gateway": gateway,
        "bridge": bridge,
        "openlist_client": openlist_client,
        "task_control": task_control,
        "services": services,
        "auto_scan_hours": auto_scan_hours,
    }
