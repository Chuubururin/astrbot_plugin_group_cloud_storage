"""Route registry — single source of truth for all API routes.

Each route: Route(path_suffix, methods, handler_name, description, auth_level)
auth_level: 'page' (page admin), 'db' (database token), 'none' (public)
"""
from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_group_cloud_storage"

# Route definitions: Route(suffix, methods, handler, description, auth)
from collections.abc import Callable, Iterable  # noqa: E402  (constants first for readability)
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class Route:
    suffix: str
    methods: tuple[str, ...]
    handler: str
    description: str
    auth: str


ROUTES = [
    # Groups
    Route("groups", ("GET",), "api_groups", "群清单", "page"),
    Route("accounts", ("GET",), "api_accounts", "账号清单", "page"),
    Route("groups/scan", ("POST",), "api_scan", "触发群扫描", "page"),
    Route("groups/batch", ("POST",), "api_groups_batch_update", "批量改名/标号", "page"),
    Route("groups/batch-ops", ("POST",), "api_groups_batch_actions", "批量群操作", "page"),
    Route("groups/order", ("POST",), "api_groups_order", "群排序", "page"),
    Route("groups/remove", ("POST",), "api_groups_remove", "移除管理条目", "page"),
    Route("groups/removed", ("GET",), "api_groups_removed", "已移除管理的群", "page"),
    Route("groups/restore", ("POST",), "api_groups_restore", "恢复管理", "page"),
    Route("groups/open-state", ("GET",), "api_groups_open_state", "群可访问性预检", "page"),
    Route("groups/info", ("GET",), "api_group_info", "群信息", "page"),
    Route("groups/members", ("GET",), "api_group_members", "群成员", "page"),
    Route("groups/honor", ("GET",), "api_group_honor", "群荣誉", "page"),
    Route("groups/system-msg", ("GET",), "api_group_system_msg", "群系统消息", "page"),
    # Files
    Route("files", ("GET",), "api_files", "文件列表/检索", "page"),
    Route("stat", ("GET",), "api_stat", "群统计", "page"),
    Route("events", ("GET",), "api_queue_events", "操作队列 SSE", "page"),
    Route("files/upload/prepare", ("POST",), "api_file_upload_prepare", "上传准备", "page"),
    Route("files/recommend-group", ("GET",), "api_files_recommend_group", "推荐上传群", "page"),
    Route("files/upload/<token>", ("POST",), "api_file_upload", "上传文件", "page"),
    Route("files/delete", ("POST",), "api_file_delete", "删除文件", "page"),
    Route("files/replace_name", ("POST",), "api_file_replace_name", "改名重传", "page"),
    Route("files/convert-volumes", ("POST",), "api_file_convert_volumes", "手动分卷", "page"),
    Route("albums/media", ("GET",), "api_album_media", "相册媒体", "page"),
    Route("albums/detail", ("GET",), "api_album_detail", "相册详情", "page"),
    Route("albums/media/delete", ("POST",), "api_album_media_delete", "删除相册媒体", "page"),
    Route("albums/media/comment", ("POST",), "api_album_media_comment", "相册媒体评论", "page"),
    Route("albums/video-preview", ("POST",), "api_album_video_preview", "视频预览", "page"),
    Route("albums/create", ("POST",), "api_album_create", "创建相册（OneBot模板）", "page"),
    Route("essence/save", ("POST",), "api_essence_save", "精华保存", "page"),
    Route("essence/text", ("GET",), "api_essence_text", "精华全文", "page"),
    Route("essence/delete", ("POST",), "api_essence_delete", "精华删除", "page"),
    Route("fetch", ("POST",), "api_fetch", "外部文件导入", "page"),
    Route("files/tags", ("POST",), "api_file_tags", "资源标签", "page"),
    Route("files/tagcloud", ("GET",), "api_tagcloud", "标签云", "page"),
    Route("files/move", ("POST",), "api_file_move", "移动文件", "page"),
    Route("files/download", ("GET",), "api_file_download", "文件直链", "page"),
    Route("files/link", ("GET",), "api_file_link", "下载直链", "page"),
    Route("download/address", ("GET",), "api_download_address", "下载服务地址", "page"),
    Route("files/folder-create", ("POST",), "api_folder_create", "新建目录", "page"),
    Route("files/folder-delete", ("POST",), "api_folder_delete", "删除目录", "page"),
    Route("files/folder-rename", ("POST",), "api_folder_rename", "目录改名", "page"),
    Route("files/uri", ("GET",), "api_file_uri", "URI定位", "page"),
    Route("files/scan", ("POST",), "api_files_scan", "文件扫描", "page"),
    Route("files/sync", ("POST",), "api_files_sync", "单群刷新", "page"),
    Route("files/batch-delete", ("POST",), "api_files_batch_delete", "批量删除", "page"),
    Route("files/batch-move", ("POST",), "api_files_batch_move", "批量移动", "page"),
    Route("files/batch-tags", ("POST",), "api_files_batch_tags", "批量标签", "page"),
    Route("files/links", ("POST",), "api_files_links", "批量直链", "page"),
    Route("files/detail", ("GET",), "api_file_detail", "文件详情", "page"),
    # Preview
    Route("preview/policy", ("GET",), "api_preview_policy", "预览策略", "page"),
    Route("meta/classify", ("GET",), "api_meta_classify", "类型分类", "page"),
    # Tasks
    Route("tasks", ("GET", "POST"), "api_tasks", "任务记录", "page"),
    Route("tasks/queue", ("GET",), "api_tasks_queue", "队列状态", "page"),
    Route("tasks/pause", ("POST",), "api_tasks_pause", "暂停任务", "page"),
    Route("tasks/resume", ("POST",), "api_tasks_resume", "继续任务", "page"),
    Route("tasks/interrupt", ("POST",), "api_tasks_interrupt", "中断任务", "page"),
    Route("tasks/undo", ("POST",), "api_tasks_undo", "撤销任务", "page"),
    Route("tasks/ops", ("POST",), "api_tasks_ops", "操作流记录", "page"),
    Route("tasks/resume-pending", ("POST",), "api_tasks_resume_pending", "断点续传", "page"),
    # Config
    Route("config/get", ("GET",), "api_config_get", "配置查询", "page"),
    Route("config/save", ("POST",), "api_config_save", "配置保存", "page"),
    Route("config/reload", ("POST",), "api_config_reload", "配置热重载", "page"),
    # Database administration (POST-only: the admin token travels in the
    # request body, never in URLs/headers; read ops follow the same
    # body-carried-parameters convention as tasks/netdisk)
    Route("database/health", ("POST",), "api_database_health", "数据库健康", "db"),
    Route("database/integrity", ("POST",), "api_database_integrity", "数据库完整性", "db"),
    Route("database/backups", ("POST",), "api_database_backups", "数据库备份列表", "db"),
    Route("database/backup", ("POST",), "api_database_backup", "数据库备份", "db"),
    Route("database/restore", ("POST",), "api_database_restore", "数据库恢复", "db"),
    # Sync
    Route("sync/withering", ("POST",), "api_sync_withering", "凋零对账", "page"),
    Route("sync/status", ("GET",), "api_sync_status", "对账状态", "page"),
    # Distribution / bridge / netdisk domains
    Route("files/distribute", ("POST",), "api_files_distribute", "文件下载分发", "page"),
    Route("albums/distribute", ("POST",), "api_albums_distribute", "相册媒体下载分发", "page"),
    Route("essence/distribute", ("POST",), "api_essence_distribute", "精华全文下载分发", "page"),
    Route("netdisk/distribute", ("POST",), "api_netdisk_distribute", "网盘文件下载分发", "page"),
    Route("bridge/status", ("GET",), "api_bridge_status", "Bridge status and capability", "page"),
    Route("bridge/config/get", ("GET",), "api_bridge_config_get", "Get OpenList bridge configuration", "page"),
    Route("bridge/config/save", ("POST",), "api_bridge_config_save", "Save OpenList bridge configuration", "page"),
    Route("bridge/transfer", ("POST",), "api_bridge_transfer", "Archive group file to OpenList", "page"),
    Route("bridge/tasks", ("POST",), "api_bridge_tasks", "Bridge task list", "page"),
    Route("bridge/netdisk", ("POST",), "api_bridge_netdisk", "Browse OpenList netdisk", "page"),
    Route("bridge/task", ("GET",), "api_bridge_task", "桥接单任务查询", "page"),
    Route("bridge/cancel", ("POST",), "api_bridge_cancel", "Cancel bridge task", "page"),
    Route("bridge/retry", ("POST",), "api_bridge_retry", "Retry failed bridge task", "page"),
    Route("bridge/transfer-in", ("POST",), "api_bridge_transfer_in", "Transfer file from OpenList", "page"),
    Route("bridge/archived", ("GET",), "api_bridge_archived", "Get archived resource IDs", "page"),
    Route("netdisk/meta", ("POST",), "api_netdisk_meta", "网盘文件标记", "page"),
    Route("netdisk/link", ("POST",), "api_netdisk_link", "网盘直链", "page"),
    Route("netdisk/index", ("POST",), "api_netdisk_index", "网盘深度索引", "page"),
    Route("netdisk/upload-url", ("POST",), "api_netdisk_upload_url", "网盘 URL 上传", "page"),
    Route("netdisk/mkdir", ("POST",), "api_netdisk_mkdir", "Create directory", "page"),
    Route("netdisk/rename", ("POST",), "api_netdisk_rename", "Rename path", "page"),
    Route("netdisk/remove", ("POST",), "api_netdisk_remove", "Remove paths", "page"),
    Route("netdisk/move", ("POST",), "api_netdisk_move", "Move paths", "page"),
    Route("netdisk/copy", ("POST",), "api_netdisk_copy", "Copy paths", "page"),
    Route("netdisk/remove-empty-dirs", ("POST",), "api_netdisk_remove_empty_dirs", "Remove empty directories", "page"),
    Route("netdisk/recursive-move", ("POST",), "api_netdisk_recursive_move", "Recursively move paths", "page"),
    Route("netdisk/rename-batch", ("POST",), "api_netdisk_rename_batch", "Batch rename paths", "page"),
]


class RouteRegistry:
    """Small, defensive registry for declarative route catalogs.

    Registration is deliberately explicit: duplicate path/method pairs fail before
    touching the host context, while handler lookup remains late-bound so existing
    module-level monkeypatch seams continue to work.
    """

    def __init__(self, routes: Iterable[Route] = ROUTES):
        self.routes = tuple(routes)
        self._validate()

    def _validate(self) -> None:
        seen: set[tuple[str, str]] = set()
        for route in self.routes:
            if not route.suffix or route.suffix.startswith("/"):
                raise ValueError(f"invalid route suffix: {route.suffix!r}")
            if not route.handler or not route.description or route.auth not in {"page", "db", "none"}:
                raise ValueError(f"invalid route metadata: {route.suffix!r}")
            normalized = tuple(str(method).upper() for method in route.methods)
            if not normalized:
                raise ValueError(f"route has no methods: {route.suffix!r}")
            for method in normalized:
                key = (route.suffix, method)
                if key in seen:
                    raise ValueError(f"duplicate route: {method} {route.suffix}")
                seen.add(key)

    def register(self, context, plugin_name: str, handler_lookup: Callable[[str], object]) -> None:
        """Register every route, resolving handlers only at registration time."""
        for route in self.routes:
            handler = handler_lookup(route.handler)
            # handler_lookup must return None for undefined names (see
            # webapi._route_handler_lookup): a missing handler has to fail here,
            # while routes are wired, instead of surfacing as a request-time 500.
            if handler is None:
                raise LookupError(f"route handler not found: {route.handler}")
            context.register_web_api(
                f"/{plugin_name}/{route.suffix}", handler, list(route.methods), route.description
            )



def validate_routes(routes: Iterable[Route] = ROUTES) -> None:
    """Validate a route catalog without registering anything."""
    RouteRegistry(routes)
