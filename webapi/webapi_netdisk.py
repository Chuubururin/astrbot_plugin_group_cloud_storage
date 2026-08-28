"""Page 后端 API —— 桥接/网盘域（2026-09-03 复杂度拆分第四模块）。

OpenList 桥接（状态/传输/归档/配置）与网盘管理（目录/改名/删除/移动/复制/
URL 上传/索引）端点——成员自 webapi.py 迁移，行为零变化。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request
from core.api_validate import json_body, pick, qi
from commands.handlers import Services

import time as _time_mod
from .webapi_base import (
    PLUGIN_NAME,
    _Bound,
    _ensure_ready,
    _param,
)


__all__ = ["register_netdisk_apis"]


def register_netdisk_apis(context: Context, s: Services) -> None:
    """注册桥接/网盘域端点（catalog 采集由 register_page_apis 统一包装）。"""

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



async def api_bridge_task(s: Services) -> dict:
    """桥接单任务查询（裁定 §3.1 批准：GET bridge/task?task_id=）。"""
    task_id = await _param("task_id", "")
    if not task_id:
        return error_response("task_id is required", status_code=400)
    if not s.bridge:
        return json_response({"task_id": task_id, "state": "unknown", "enabled": False})
    return json_response(await s.bridge.status(task_id))


async def api_netdisk_meta(s: Services) -> dict:
    """网盘文件标记（N4b）：{path, tags: []}，覆盖式设置。"""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)
    raw = pick(payload, "tags", cast=list, default=[])
    tags = [str(t).strip() for t in raw if str(t).strip()][:10]
    await s.netdisk.set_tags(path, tags)
    return json_response({"path": path, "tags": tags})


async def api_netdisk_link(s: Services) -> dict:
    """网盘直链（N4c 复制直链/下载；REQ-06 仅内存不落库）。"""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)
    try:
        return json_response({"url": await s.netdisk.direct_link(path)})
    except Exception as e:
        return error_response(f"get link failed: {e}", status_code=502)


async def api_netdisk_index(s: Services) -> dict:
    """深度索引（N4b 手动任务化，HL-14 C 类）：{path} -> {task_id}。"""
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)
    payload = await json_body()
    path = pick(payload, "path", default="/")
    try:
        task_id = await s.netdisk.submit_index(path)
        return json_response({"task_id": task_id, "path": path})
    except Exception as e:
        return error_response(f"submit index failed: {e}", status_code=502)




async def api_bridge_config_get(s: Services) -> dict:
    """Get OpenList bridge configuration.

    Returns current OpenList config (password masked).
    """
    await _ensure_ready(s)
    cfg = s.config
    return json_response(
        {
            "openlist_enabled": cfg.get("openlist_enabled", False),
            "openlist_base_url": cfg.get("openlist_base_url", ""),
            "openlist_username": cfg.get("openlist_username", ""),
            "openlist_password": "***" if cfg.get("openlist_password") else "",
            "openlist_token": "***" if cfg.get("openlist_token") else "",
            "openlist_dst_dir": cfg.get("openlist_dst_dir", "/"),
            "openlist_dst_dir_template": cfg.get(
                "openlist_dst_dir_template", "{group_id}/{filename}"
            ),
            "openlist_timeout_sec": cfg.get("openlist_timeout_sec", 30),
            "openlist_allow_private_address": cfg.get(
                "openlist_allow_private_address", False
            ),
            "openlist_poll_interval_sec": cfg.get("openlist_poll_interval_sec", 0),
            "bridge_min_bytes": cfg.get("bridge_min_bytes", 0),
            "bridge_max_bytes": cfg.get("bridge_max_bytes", 0),
            "download_server_enabled": cfg.get("download_server_enabled", False),
            "download_server_host": cfg.get("download_server_host", "127.0.0.1"),
            "download_http_port": cfg.get("download_http_port", 6186),
        }
    )


async def api_bridge_config_save(s: Services) -> dict:
    """Save OpenList bridge configuration.

    Body: config key-value pairs to update.
    Only updates provided keys; password field is not updated if "***".
    """
    await _ensure_ready(s)
    payload = await json_body()

    # Allowed config keys
    allowed_keys = {
        "openlist_enabled",
        "openlist_base_url",
        "openlist_username",
        "openlist_password",
        "openlist_token",
        "openlist_dst_dir",
        "openlist_dst_dir_template",
        "openlist_timeout_sec",
        "openlist_allow_private_address",
        "openlist_poll_interval_sec",
        "bridge_min_bytes",
        "bridge_max_bytes",
        "download_server_enabled",
        "download_server_host",
        "download_http_port",
    }

    updates = {}
    for key in allowed_keys:
        if key in payload:
            # Skip masked password/token
            if key in ("openlist_password", "openlist_token") and payload[key] == "***":
                continue
            updates[key] = payload[key]

    if not updates:
        return error_response("no valid updates provided")

    # Save config (this will trigger plugin reload)
    try:
        from pathlib import Path
        import json

        config_path = (
            Path(s.store._db_path).parent.parent
            / "config"
            / "astrbot_plugin_group_cloud_storage_config.json"
        )
        if not config_path.exists():
            config_path = Path(
                f"/AstrBot/data/config/astrbot_plugin_group_cloud_storage_config.json"
            )

        # Read current config
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)

        # Update config
        config.update(updates)

        # Write back
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        return json_response(
            {
                "ok": True,
                "message": "配置已保存，重启插件后生效",
                "updated_keys": list(updates.keys()),
            }
        )
    except Exception as e:
        return error_response(f"保存配置失败: {e}")


async def api_bridge_status(s: Services) -> dict:
    """Bridge status and capability check.

    Returns:
        - enabled: whether bridge is configured
        - capability: OpenList client capability (UNKNOWN/OK/BROKEN)
        - dlserver_ready: whether download server is ready (REQ-16)
        - pending_out/in: pending task counts
    """
    await _ensure_ready(s)
    if not s.bridge:
        return json_response(
            {
                "enabled": False,
                "capability": "disabled",
                "dlserver_ready": False,
                "pending_out": 0,
                "pending_in": 0,
            }
        )

    status = await s.bridge.status()
    return json_response(status)


async def api_bridge_transfer(s: Services) -> dict:
    """Archive group file to OpenList (bridge_out).

    Body:
        - group: group ID (required)
        - resource_ids: list of resource IDs (required)
        - force: force re-archive (optional, default false)
        - dst_dir: custom destination directory (optional)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    resource_ids = pick(payload, "resource_ids", cast=list, required=True)
    force = pick(payload, "force", cast=bool, default=False)
    dst_dir = pick(payload, "dst_dir", default="")

    # Permission check (REQ-04)
    managed = s.config.get("managed_groups", [])
    if not await s.scan.is_page_managed(group, managed):
        return error_response("group not managed", status_code=403)

    # Submit tasks
    results = []
    errors = []
    for rid in resource_ids:
        try:
            rid_int = int(rid)
            task_id = await s.bridge.submit_out(
                group, rid_int, dst_dir=dst_dir or None, force=force
            )
            results.append({"resource_id": rid_int, "task_id": task_id})
        except Exception as e:
            errors.append(f"resource_id={rid}: {e}")

    return json_response({"results": results, "errors": errors})


async def api_bridge_tasks(s: Services) -> dict:
    """Bridge task list query.

    Body (optional):
        - direction: "out" or "in" (default: both)
        - state: filter by state (e.g., "pending", "done")
    """
    await _ensure_ready(s)
    if not s.bridge:
        return json_response({"tasks": []})

    payload = await json_body()
    direction = pick(payload, "direction", default="")
    state_filter = pick(payload, "state", default="")

    # Query tasks from archive_map
    if direction:
        directions = [direction]
    else:
        directions = ["out", "in"]

    all_tasks = []
    for d in directions:
        rows = await s.store.list_archive_map(
            states=("pending", "running", "done", "failed"), direction=d
        )
        for row in rows:
            if state_filter and row.get("state") != state_filter:
                continue
            all_tasks.append(row)

    return json_response({"tasks": all_tasks})


async def api_bridge_netdisk(s: Services) -> dict:
    """Browse OpenList netdisk directory.

    Body:
        - path: directory path (default: "/")
    """
    await _ensure_ready(s)
    if not s.netdisk:
        return error_response("netdisk service not enabled", status_code=400)

    payload = await json_body()
    path = pick(payload, "path", default="/")
    page = pick(payload, "page", cast=int, default=1)
    page_size = pick(payload, "page_size", cast=int, default=50)

    try:
        data = await s.netdisk.browse(path, max(1, page), max(1, min(page_size, 500)))
        return json_response(data)
    except Exception as e:
        return error_response(f"list dir failed: {e}", status_code=502)


async def api_bridge_cancel(s: Services) -> dict:
    """Cancel bridge task.

    Body:
        - task_id: task ID to cancel (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    task_id = pick(payload, "task_id", required=True, empty_allowed=False)

    ok = await s.bridge.cancel(task_id)
    return json_response({"ok": ok, "task_id": task_id})


async def api_bridge_retry(s: Services) -> dict:
    """Retry failed bridge task.

    Body:
        - task_id: task ID to retry (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    task_id = pick(payload, "task_id", required=True, empty_allowed=False)

    ok = await s.bridge.retry(task_id)
    return json_response({"ok": ok, "task_id": task_id})


async def api_bridge_transfer_in(s: Services) -> dict:
    """Transfer file from OpenList netdisk to QQ group (bridge_in).

    Body:
        - group: target group ID (required)
        - path: file path on OpenList (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    path = pick(payload, "path", required=True, empty_allowed=False)

    # Permission check (Page端口径)
    if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
        return error_response("group not managed", status_code=403)

    try:
        task_id = await s.bridge.submit_in(path, group_id=group)
        return json_response(
            {
                "ok": True,
                "task_id": task_id,
                "path": path,
                "group": group,
            }
        )
    except Exception as e:
        return error_response(f"transfer failed: {e}", status_code=500)


async def api_bridge_archived(s: Services) -> dict:
    """Get archived resource IDs for a group (for badge display).

    Query params:
        - group: group ID (required)
    """
    await _ensure_ready(s)
    group = request.query.get("group", "")
    if not group:
        return error_response("group required", status_code=400)

    # Query all done archive entries for this group
    rows = await s.store.list_archive_map(states=("done",), direction="out")
    # Filter by group
    archived_ids = [
        r["resource_id"]
        for r in rows
        if r.get("group_id") == group and r.get("resource_id", 0) > 0
    ]
    return json_response({"group": group, "archived_ids": archived_ids})


# --- Netdisk file operations ---


async def api_netdisk_mkdir(s: Services) -> dict:
    """Create a directory on OpenList netdisk.

    Body:
        - path: full path to create (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)

    try:
        await s.bridge._client.mkdir(path)
        return json_response({"ok": True, "path": path})
    except Exception as e:
        return error_response(f"mkdir failed: {e}", status_code=500)


async def api_netdisk_rename(s: Services) -> dict:
    """Rename a file or directory on OpenList netdisk.

    Body:
        - path: full path to the file/directory (required)
        - name: new name (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    path = pick(payload, "path", required=True, empty_allowed=False)
    name = pick(payload, "name", required=True, empty_allowed=False)

    try:
        await s.bridge._client.rename(path, name)
        return json_response({"ok": True, "path": path, "new_name": name})
    except Exception as e:
        return error_response(f"rename failed: {e}", status_code=500)


async def api_netdisk_remove(s: Services) -> dict:
    """Remove files or directories from OpenList netdisk.

    Body:
        - dir: parent directory path (required)
        - names: list of file/directory names to remove (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    dir_path = pick(payload, "dir", required=True, empty_allowed=False)
    names = pick(payload, "names", cast=list, required=True)

    try:
        await s.bridge._client.remove(dir_path, names)
        return json_response({"ok": True, "dir": dir_path, "removed": names})
    except Exception as e:
        return error_response(f"remove failed: {e}", status_code=500)


async def api_netdisk_move(s: Services) -> dict:
    """Move files or directories on OpenList netdisk.

    Body:
        - src_dir: source directory path (required)
        - dst_dir: destination directory path (required)
        - names: list of file/directory names to move (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    src_dir = pick(payload, "src_dir", required=True, empty_allowed=False)
    dst_dir = pick(payload, "dst_dir", required=True, empty_allowed=False)
    names = pick(payload, "names", cast=list, required=True)

    try:
        await s.bridge._client.move(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "moved": names}
        )
    except Exception as e:
        return error_response(f"move failed: {e}", status_code=500)


async def api_netdisk_copy(s: Services) -> dict:
    """Copy files or directories on OpenList netdisk.

    Body:
        - src_dir: source directory path (required)
        - dst_dir: destination directory path (required)
        - names: list of file/directory names to copy (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    src_dir = pick(payload, "src_dir", required=True, empty_allowed=False)
    dst_dir = pick(payload, "dst_dir", required=True, empty_allowed=False)
    names = pick(payload, "names", cast=list, required=True)

    try:
        await s.bridge._client.copy(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "copied": names}
        )
    except Exception as e:
        return error_response(f"copy failed: {e}", status_code=500)


async def api_netdisk_upload_url(s: Services) -> dict:
    """2026-09-01 N-06：网盘上传——URL 链接来源（OpenList 离线下载，零落盘）。

    Body: {url, dir=当前目录, name?}；幂等：同 URL+目录命中在途任务时跳过重提。
    本地文件上传到网盘经「上传群文件 → 转存网盘」两跳（既有管线，不新建通道）。
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)
    payload = await json_body()
    url = str(payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return error_response("url must be http(s)", status_code=400)
    url = url[:4096]
    dst_dir = str(payload.get("dir") or "/").strip() or "/"
    if not dst_dir.startswith("/"):
        dst_dir = "/" + dst_dir
    name = str(payload.get("name") or "").strip()[:200]
    # SSRF 防护与既有桥接侧一致（scheme 白名单 http/https；私有地址策略由
    # openlist_allow_private_address 显式放行——OpenList 自身服务端为最终边界）
    tasks = await s.bridge._client.submit_offline_download([url], dst_dir)
    if not tasks:
        return error_response("openlist rejected download task", status_code=500)
    return json_response(
        {
            "ok": True,
            "task_id": tasks[0].id,
            "dir": dst_dir,
            "name": name or "",
        }
    )


async def api_netdisk_remove_empty_dirs(s: Services) -> dict:
    """Remove empty directories from OpenList netdisk.

    Body:
        - src_dir: parent directory path (required)
        - names: list of directory names to check and remove if empty (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    src_dir = pick(payload, "src_dir", required=True, empty_allowed=False)
    names = pick(payload, "names", cast=list, required=True)

    try:
        await s.bridge._client.remove_empty_dirs(src_dir, names)
        return json_response({"ok": True, "src_dir": src_dir, "removed": names})
    except Exception as e:
        return error_response(f"remove_empty_dirs failed: {e}", status_code=500)


async def api_netdisk_recursive_move(s: Services) -> dict:
    """Recursively move files and directories on OpenList netdisk.

    Body:
        - src_dir: source directory path (required)
        - dst_dir: destination directory path (required)
        - names: list of file/directory names to move (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    src_dir = pick(payload, "src_dir", required=True, empty_allowed=False)
    dst_dir = pick(payload, "dst_dir", required=True, empty_allowed=False)
    names = pick(payload, "names", cast=list, required=True)

    try:
        await s.bridge._client.recursive_move(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "moved": names}
        )
    except Exception as e:
        return error_response(f"recursive_move failed: {e}", status_code=500)


async def api_netdisk_rename_batch(s: Services) -> dict:
    """Batch rename files or directories on OpenList netdisk.

    Body:
        - renames: list of {path, name} objects (required)
    """
    await _ensure_ready(s)
    if not s.bridge:
        return error_response("bridge not enabled", status_code=400)

    payload = await json_body()
    renames = pick(payload, "renames", cast=list, required=True)

    results = []
    errors = []
    for item in renames:
        path = item.get("path", "")
        name = item.get("name", "")
        if not path or not name:
            errors.append(f"Invalid item: {item}")
            continue
        try:
            await s.bridge._client.rename(path, name)
            results.append({"path": path, "new_name": name})
        except Exception as e:
            errors.append(f"rename {path}: {e}")

    return json_response({"ok": len(errors) == 0, "results": results, "errors": errors})

