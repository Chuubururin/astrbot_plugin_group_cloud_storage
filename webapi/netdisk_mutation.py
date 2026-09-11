"""Netdisk mutation handlers (upload, mkdir, rename, remove, move, copy, etc.)."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.web import error_response, json_response
from core.api_validate import json_body, pick
from commands.handlers import Services

from .webapi_base import _ensure_ready


__all__ = [
    "api_bridge_config_save",
    "api_netdisk_upload_url",
    "api_netdisk_mkdir",
    "api_netdisk_rename",
    "api_netdisk_remove",
    "api_netdisk_move",
    "api_netdisk_copy",
    "api_netdisk_remove_empty_dirs",
    "api_netdisk_recursive_move",
    "api_netdisk_rename_batch",
]


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
        "bridge_min_size",
        "bridge_max_size",
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
        from .config import _read_plugin_config, _write_plugin_config

        config = _read_plugin_config()
        config.update(updates)
        _write_plugin_config(config)

        return json_response(
            {
                "ok": True,
                "message": "配置已保存，重启插件后生效",
                "updated_keys": list(updates.keys()),
            }
        )
    except Exception as e:
        logger.warning(f"[webapi] 保存配置失败: {e}", exc_info=True)
        return error_response("保存配置失败")


# --- Netdisk file operations ---


async def api_netdisk_upload_url(s: Services) -> dict:
    """Upload to the netdisk from a URL source (OpenList offline download;
    nothing is written to local disk).

    Body: {url, dir=current directory, name?}.
    Local files reach the netdisk through the existing two-hop pipeline
    (upload as a group file, then transfer to the netdisk).
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
    # SSRF protection: scheme whitelist http/https only; private-address access
    # is gated by openlist_allow_private_address (the OpenList server itself is
    # the final boundary)
    tasks = await s.bridge.submit_offline_download([url], dst_dir)
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
        await s.bridge.mkdir(path)
        return json_response({"ok": True, "path": path})
    except Exception as e:
        logger.warning(f"[webapi] mkdir failed: {e}", exc_info=True)
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
        await s.bridge.rename(path, name)
        return json_response({"ok": True, "path": path, "new_name": name})
    except Exception as e:
        logger.warning(f"[webapi] rename failed: {e}", exc_info=True)
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
        await s.bridge.remove(dir_path, names)
        return json_response({"ok": True, "dir": dir_path, "removed": names})
    except Exception as e:
        logger.warning(f"[webapi] remove failed: {e}", exc_info=True)
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
        await s.bridge.move(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "moved": names}
        )
    except Exception as e:
        logger.warning(f"[webapi] move failed: {e}", exc_info=True)
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
        await s.bridge.copy(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "copied": names}
        )
    except Exception as e:
        logger.warning(f"[webapi] copy failed: {e}", exc_info=True)
        return error_response(f"copy failed: {e}", status_code=500)


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
        await s.bridge.remove_empty_dirs(src_dir, names)
        return json_response({"ok": True, "src_dir": src_dir, "removed": names})
    except Exception as e:
        logger.warning(f"[webapi] remove_empty_dirs failed: {e}", exc_info=True)
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
        await s.bridge.recursive_move(src_dir, dst_dir, names)
        return json_response(
            {"ok": True, "src_dir": src_dir, "dst_dir": dst_dir, "moved": names}
        )
    except Exception as e:
        logger.warning(f"[webapi] recursive_move failed: {e}", exc_info=True)
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
            await s.bridge.rename(path, name)
            results.append({"path": path, "new_name": name})
        except Exception as e:
            logger.warning(f"[webapi] rename {path}: {e}", exc_info=True)
            errors.append(f"rename {path}: {e}")

    return json_response({"ok": len(errors) == 0, "results": results, "errors": errors})
