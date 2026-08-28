"""命令处理器薄壳（DoD #2：parse → authorize → service，不直接碰 DB/OneBot）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from core.domain.enums import SyncStatus
from core.services.permission import PermissionService
from core.services.resource_query import ResourceQueryService, StatsService
from core.services.resource_sync import ResourceSyncService
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort


@dataclass
class Services:
    """服务组合，由 main.py 装配注入。"""

    permission: PermissionService
    store: MetaStorePort
    api: OneBotApiPort
    sync: ResourceSyncService
    query: ResourceQueryService
    stats: StatsService
    sync_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Page/群管理（docs/09 §12）——向后兼容可选注入
    scan: "GroupScanService | None" = None
    ops: "FileOpsService | None" = None
    planner: "StoragePlanner | None" = None
    searchkv: "SearchKV | None" = None
    queue: "OpQueue | None" = None
    ingest: "CloudIngestService | None" = None
    transfer: "TransferService | None" = None
    dlserver: "DownloadServerService | None" = None
    gateway: "StorageGateway | None" = None
    bridge: "BridgeService | None" = None  # OpenList bridge (REQ-08)
    netdisk: "NetdiskService | None" = None  # 网盘浏览/登记/索引（ADR-0004）
    task_control: "TaskControlService | None" = None  # v15：任务台账与控制（D-6）
    distributor: "DistributorService | None" = None  # 2026-09-02 W2-A：下载分发编排
    converter: "ConverterService | None" = None  # 2026-09-02 W2-B：格式转换
    config: dict = field(default_factory=dict)
    ready: "Callable[[], Awaitable[None]] | None" = None  # 惰性初始化（Page API 首调）
    # 在线账号查询（由 main.py 注入，返回当前在线的 account_id 集合）
    get_online_account_ids: "Callable[[], set[str]] | None" = None

    def lock_for(self, group_id: str) -> asyncio.Lock:
        return self.sync_locks.setdefault(group_id, asyncio.Lock())


def _err(msg: str) -> str:
    return f"❌ {msg}"


async def handle_cssync(event, services: Services, group_id: str = "") -> str:
    """解析群号（默认当前群）→ 授权 → 全量同步 → 统计报告。"""
    actual_group = event.get_group_id()
    target = group_id or actual_group
    if not target:
        return _err("无法确定群号。")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target, actual_group
    ):
        return _err("权限不足（本群需群管理员；跨群需全局管理员且在受管群内）。")
    result = await services.sync.run_full_sync(target, services.lock_for(target))
    if result.status == SyncStatus.FAILED:
        return _err(f"同步失败：{result.error}")
    if result.status == SyncStatus.CANCELLED:
        return "⚠️ 同步已取消。"
    stats = await services.stats.stats(target)
    return (
        StatsService.format_stats(stats)
        + f"\n▸ 本次：发现 {result.files_found} / 入库 {result.files_indexed}"
    )


async def handle_csfiles(
    event, services: Services, group_id: str = "", page: int = 1
) -> str:
    actual_group = event.get_group_id()
    target = group_id or actual_group
    if not target:
        return _err("无法确定群号。")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target, actual_group
    ):
        return _err("权限不足。")
    pg = await services.query.page(target, page=page)
    return StatsService.format_page(pg)


async def handle_csfile(event, services: Services, id: int, group_id: str = "") -> str:
    actual_group = event.get_group_id()
    target = group_id or actual_group
    if not target:
        return _err("无法确定群号。")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target, actual_group
    ):
        return _err("权限不足。")
    row = await services.query.detail(target, id)
    if not row:
        return _err(f"群 {target} 中不存在文件 ID={id}。")
    text = StatsService.format_detail(row)
    # 动态直链（AC10：不持久化 url）
    try:
        url = await services.api.get_group_file_url(
            target, row["source_ref"], row["busid"] or 0, row["name"]
        )
        text += f"\n▸ 直链：{url}\n（链接有时效，请及时下载）"
    except Exception as e:
        text += f"\n▸ 直链获取失败：{e}"
    return text


async def handle_cssave(
    event, services: Services, group_id: str = "", title: str = "", text: str = ""
) -> str:
    """文本保存为群精华（v1.2）：长文本自动分段（每段 ≤4500 字）逐段发送并设精。"""
    actual_group = event.get_group_id()
    target = group_id or actual_group
    if not target:
        return _err("无法确定群号。")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target, actual_group
    ):
        return _err("权限不足。")
    if not services.ingest:
        return _err("入库服务未就绪。")
    if not title.strip() or not text.strip():
        return _err("用法：/cssave [群号] <标题> <正文>（正文可含空格）")
    try:
        task_id = await services.ingest.submit_essence_save(target, title.strip(), text)
    except ValueError as e:
        return _err(str(e))
    return (
        f"▸ 文本保存为群精华已排队：{title.strip()}\n"
        f"▸ 任务 {task_id}（长文本将自动分段存储；状态可经插件页面查看）"
    )


async def handle_csfetch(
    event, services: Services, group_id: str = "", url: str = "", name: str = ""
) -> str:
    """HTTP/HTTPS/FTP 外部文件入库（v1.2）：非本机文件多途径传输至目标群。"""
    actual_group = event.get_group_id()
    target = group_id or actual_group
    if not target:
        return _err("无法确定群号。")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target, actual_group
    ):
        return _err("权限不足。")
    if not services.ingest:
        return _err("入库服务未就绪。")
    if not url:
        return _err("用法：/csfetch [群号] <http|https|ftp URL> [文件名]")
    try:
        task_id = await services.ingest.submit_fetch(
            target, url, name.strip() if name else ""
        )
    except ValueError as e:
        return _err(str(e))
    return f"▸ 外部文件入库已排队：{url}\n▸ 任务 {task_id}"


async def handle_csarchive(
    event, services: Services, group_id: str = "", file_ref: str = "", force: bool = False
) -> str:
    """Archive group file to OpenList (bridge_out)."""
    actual_group = event.get_group_id()
    target_group = group_id or actual_group
    if not target_group:
        return _err("Cannot determine group ID.")
    if not services.permission.can_manage(
        event.get_sender_id(), _role(event), target_group, actual_group
    ):
        return _err("Insufficient permission (group admin required).")
    if not services.bridge:
        return _err("Bridge service not configured (openlist_enabled=false).")
    if not file_ref:
        return _err("Usage: /csarchive [group_id] <file_id_or_name> [--force]")

    # Resolve resource ID
    try:
        rid = int(file_ref)
    except (TypeError, ValueError):
        # Try to find by name
        page = await services.query.page(target_group, page=1)
        found = None
        for item in page.items:
            if file_ref.lower() in item.name.lower():
                found = item
                break
        if not found:
            return _err(f"File not found: {file_ref}")
        rid = found.id

    try:
        task_id = await services.bridge.submit_out(target_group, rid, force=force)
    except Exception as e:
        return _err(f"Submit failed: {e}")

    return (
        f"Archive task submitted: {task_id}\n"
        f"File ID: {rid}, Group: {target_group}\n"
        f"Use /csbridge status to check progress."
    )


async def handle_csbridge(
    event, services: Services, action: str = "", task_id: str = ""
) -> str:
    """Bridge task management (status/cancel/retry)."""
    actual_group = event.get_group_id()
    if not services.bridge:
        return _err("Bridge service not configured (openlist_enabled=false).")

    if action == "status":
        status = await services.bridge.status(task_id if task_id else None)
        if task_id:
            return f"Task {task_id}: {status.get('state', 'unknown')}"
        pending_out = status.get("pending_out", 0)
        pending_in = status.get("pending_in", 0)
        return (
            f"Bridge Status:\n"
            f"  Enabled: {status.get('enabled', False)}\n"
            f"  Capability: {status.get('capability', 'unknown')}\n"
            f"  DL Server Ready: {status.get('dlserver_ready', False)}\n"
            f"  Pending Out: {pending_out}\n"
            f"  Pending In: {pending_in}"
        )
    elif action == "cancel":
        if not task_id:
            return _err("Usage: /csbridge cancel <task_id>")
        ok = await services.bridge.cancel(task_id)
        return f"Cancel task {task_id}: {'success' if ok else 'failed'}"
    elif action == "retry":
        if not task_id:
            return _err("Usage: /csbridge retry <task_id>")
        ok = await services.bridge.retry(task_id)
        return f"Retry task {task_id}: {'success' if ok else 'failed'}"
    else:
        return (
            "Usage:\n"
            "  /csbridge status [task_id]  Query status\n"
            "  /csbridge cancel <task_id>  Cancel task\n"
            "  /csbridge retry <task_id>   Retry failed task"
        )


def handle_cshelp() -> str:
    return (
        "/cssync [group_id]  Sync group cloud storage index and stats\n"
        "/csfiles [group_id] [page]  List group files\n"
        "/csfile <id> [group_id]  File detail + download link\n"
        "/cssave [group_id] <title> <text>  Save text as group essence\n"
        "/csfetch [group_id] <URL> [filename]  Fetch external file to group\n"
        "/csarchive [group_id] <file_id> [--force]  Archive file to OpenList\n"
        "/csbridge status|cancel|retry [task_id]  Bridge task management\n"
        "/cshelp  Show this help\n"
        "Note: Management ops require group admin; cross-group requires global admin."
    )


def _role(event) -> str:
    """从事件提取群角色（owner/admin → admin）。

    注意：AstrBot 4.27 对 OneBot 消息事件不填充 event.role（恒为 member），
    需回退读取 raw_message.sender.role（OneBot 11 标准字段）。
    """
    try:
        raw = getattr(event.message_obj, "raw_message", None) or {}
        r = (raw.get("sender") or {}).get("role", "")
        if r in ("owner", "admin"):
            return "admin"
    except Exception:
        pass
    try:
        return "admin" if event.is_admin() else "member"
    except Exception:
        return "member"
