"""Page backend APIs — bridge/netdisk domain.

OpenList bridge (status/transfer/archive/config) and netdisk management
(directory/rename/remove/move/copy/URL upload/index) endpoints.
"""

from __future__ import annotations

from astrbot.api.star import Context
from commands.handlers import Services

from .webapi_base import PLUGIN_NAME, _Bound
from .netdisk_query import (
    api_bridge_archived,
    api_bridge_config_get,
    api_bridge_netdisk,
    api_bridge_status,
    api_bridge_task,
    api_netdisk_link,
    api_netdisk_meta,
)
from .netdisk_mutation import (
    api_bridge_config_save,
    api_netdisk_copy,
    api_netdisk_mkdir,
    api_netdisk_move,
    api_netdisk_recursive_move,
    api_netdisk_remove,
    api_netdisk_remove_empty_dirs,
    api_netdisk_rename,
    api_netdisk_rename_batch,
    api_netdisk_upload_url,
)
from .netdisk_transfer import (
    api_bridge_cancel,
    api_bridge_retry,
    api_bridge_tasks,
    api_bridge_transfer,
    api_bridge_transfer_in,
    api_netdisk_index,
)


__all__ = ["register_netdisk_apis"]


def register_netdisk_apis(context: Context, s: Services) -> None:
    """Register bridge/netdisk endpoints (catalog collection is wrapped
    centrally by register_page_apis)."""

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/status",
        _Bound(s, api_bridge_status),
        ["GET"],
        "Bridge status and capability",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/config/get",
        _Bound(s, api_bridge_config_get),
        ["GET"],
        "Get OpenList bridge configuration",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/config/save",
        _Bound(s, api_bridge_config_save),
        ["POST"],
        "Save OpenList bridge configuration",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/transfer",
        _Bound(s, api_bridge_transfer),
        ["POST"],
        "Archive group file to OpenList",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/tasks",
        _Bound(s, api_bridge_tasks),
        ["POST"],
        "Bridge task list",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/netdisk",
        _Bound(s, api_bridge_netdisk),
        ["POST"],
        "Browse OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/task",
        _Bound(s, api_bridge_task),
        ["GET"],
        "桥接单任务查询（task_id）",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/meta",
        _Bound(s, api_netdisk_meta),
        ["POST"],
        "网盘文件标记（tags）",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/link",
        _Bound(s, api_netdisk_link),
        ["POST"],
        "网盘直链（内存直链，不落库）",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/index",
        _Bound(s, api_netdisk_index),
        ["POST"],
        "网盘深度索引（手动任务）",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/cancel",
        _Bound(s, api_bridge_cancel),
        ["POST"],
        "Cancel bridge task",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/retry",
        _Bound(s, api_bridge_retry),
        ["POST"],
        "Retry failed bridge task",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/transfer-in",
        _Bound(s, api_bridge_transfer_in),
        ["POST"],
        "Transfer file from OpenList to QQ group",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/bridge/archived",
        _Bound(s, api_bridge_archived),
        ["GET"],
        "Get archived resource IDs for group",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/upload-url",
        _Bound(s, api_netdisk_upload_url),
        ["POST"],
        "网盘 URL 上传（OpenList 离线下载）",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/mkdir",
        _Bound(s, api_netdisk_mkdir),
        ["POST"],
        "Create directory on OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/rename",
        _Bound(s, api_netdisk_rename),
        ["POST"],
        "Rename file or directory on OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/remove",
        _Bound(s, api_netdisk_remove),
        ["POST"],
        "Remove files or directories from OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/move",
        _Bound(s, api_netdisk_move),
        ["POST"],
        "Move files or directories on OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/copy",
        _Bound(s, api_netdisk_copy),
        ["POST"],
        "Copy files or directories on OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/remove-empty-dirs",
        _Bound(s, api_netdisk_remove_empty_dirs),
        ["POST"],
        "Remove empty directories from OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/recursive-move",
        _Bound(s, api_netdisk_recursive_move),
        ["POST"],
        "Recursively move files and directories on OpenList netdisk",
    )

    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/rename-batch",
        _Bound(s, api_netdisk_rename_batch),
        ["POST"],
        "Batch rename files or directories on OpenList netdisk",
    )
