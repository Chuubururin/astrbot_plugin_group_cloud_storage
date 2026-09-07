"""Domain: Config management — handlers extracted from webapi.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body
from .webapi_base import _ensure_ready


# ---- Config center ----

# Host-side plugin config JSON (same file the bridge saves). The path is
# built ONLY from trusted anchors — the plugin's own install location
# (<root>/data/plugins/<plugin>/webapi/config.py → <root>/data/config) and
# the fixed container-layout path. No request- or store-derived data
# participates in path construction; reads/writes resolve through the
# fixed candidate whitelist and re-check the final path before I/O.
_PLUGIN_CONFIG_FILE = "astrbot_plugin_group_cloud_storage_config.json"
_FALLBACK_CONFIG_DIR = Path("/AstrBot/data/config")


def _config_candidates() -> tuple:
    """The only two paths the plugin config JSON may ever live at."""
    plugin_anchor = (
        Path(__file__).resolve().parents[3] / "config" / _PLUGIN_CONFIG_FILE
    )
    fallback = (_FALLBACK_CONFIG_DIR / _PLUGIN_CONFIG_FILE).resolve()
    return (plugin_anchor.resolve(), fallback)


def _validated_config_path() -> Path:
    """Pick the existing config path; refuse anything off the whitelist."""
    candidates = _config_candidates()
    chosen = candidates[-1]
    for candidate in candidates:
        try:
            if candidate.exists():
                chosen = candidate
                break
        except OSError:
            continue
    if chosen not in candidates or chosen.name != _PLUGIN_CONFIG_FILE:
        raise PermissionError("plugin config path escapes allowed directories")
    return chosen


def _read_plugin_config() -> dict:
    """Whitelist-checked read of the host plugin config JSON."""
    real = Path(os.path.realpath(str(_validated_config_path())))
    if ".." in real.parts or real.name != _PLUGIN_CONFIG_FILE:
        raise PermissionError("plugin config path escapes allowed directories")
    try:
        with real.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_plugin_config(data: dict) -> None:
    """Whitelist-checked write of the host plugin config JSON."""
    real = Path(os.path.realpath(str(_validated_config_path())))
    if ".." in real.parts or real.name != _PLUGIN_CONFIG_FILE:
        raise PermissionError("plugin config path escapes allowed directories")
    with real.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# Group order defines the rendering order of config tabs and is the single
# source of truth for schema grouping: the 5 user-facing groups put common
# settings first and aggregate advanced ones last (advanced items are hidden
# in the native AstrBot config page).
_CONFIG_GROUPS = [
    "基础设置",
    "自动保存与分卷",
    "下载服务",
    "网盘归档",
    "高级选项",
]


async def api_config_get(s: Services) -> dict:
    """Config center data grouped for rendering: _conf_schema groups plus
    current values (sensitive entries masked).

    Returns {groups: [{name, items: [{key, value, default, description, type}]}],
    reload_required: [keys]} so the frontend can render, search, and highlight
    dangerous items per group.
    """
    await _ensure_ready(s)
    schema_path = (
        Path(__file__).resolve().parent.parent / "_conf_schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        schema = {}
    cfg = s.config.raw if hasattr(s.config, "raw") else dict(s.config)
    reload_required = {
        "request_interval_ms", "managed_groups", "global_admin_qqs",
        "download_server_enabled", "download_server_host", "download_http_port", "download_ftp_port",
        "openlist_enabled", "openlist_base_url", "openlist_username",
        "openlist_password", "openlist_token",
    }
    groups: dict[str, list] = {g: [] for g in _CONFIG_GROUPS}
    for key, meta in schema.items():
        group = meta.get("group", "其他")
        groups.setdefault(group, [])
        value = cfg.get(key, meta.get("default"))
        item = {
            "key": key,
            "value": value,
            "default": meta.get("default"),
            "description": meta.get("description", ""),
            "type": meta.get("type", "string"),
            "group": group,
            "reload_required": key in reload_required,
            "hint": meta.get("hint", ""),
            "invisible": bool(meta.get("invisible", False)),
        }
        # Mask sensitive values (only when a value is present)
        if key in ("openlist_password", "openlist_token", "download_token"):
            if value:
                item["value"] = "***"
                item["masked"] = True
        groups[group].append(item)
    return json_response(
        {
            "groups": [
                {"name": g, "items": groups[g]} for g in _CONFIG_GROUPS if groups[g]
            ],
            "reload_required": sorted(reload_required),
        }
    )


async def api_config_save(s: Services) -> dict:
    """Config center save: write values back (unsubmitted keys keep their
    current values) and persist to the host config.

    Body: {values: {key: value}} (only changed keys are submitted); the
    sensitive-entry value "***" means "do not modify".
    Returns {saved: [keys], reload_required: [keys]}.
    """
    await _ensure_ready(s)
    payload = await json_body()
    values = (payload or {}).get("values")
    if not isinstance(values, dict) or not values:
        return error_response("values required", status_code=400)
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        schema = {}
    saved: list[str] = []
    masked_keys = {"openlist_password", "openlist_token", "download_token", "database_admin_token"}
    normalized: dict = {}
    for key, value in values.items():
        if key not in schema:
            continue
        if key in masked_keys and value == "***":
            continue
        item_value = value
        # Normalize the value type to the schema type to keep dirty values out
        typ = schema[key].get("type", "string")
        if typ == "int":
            try:
                item_value = int(value)
            except (TypeError, ValueError):
                continue
        elif typ == "float":
            try:
                item_value = float(value)
            except (TypeError, ValueError):
                continue
        elif typ == "bool":
            item_value = str(value).strip().lower() in (
                "1", "true", "yes", "on",
            )
        elif typ == "list":
            item_value = value if isinstance(value, list) else []
        elif typ == "dict":
            item_value = value if isinstance(value, dict) else {}
        normalized[key] = item_value
        saved.append(key)
    if not saved:
        return error_response("no valid keys to save", status_code=400)
    # Persist to the host plugin config JSON (same path as the bridge save)
    # and sync into the in-memory s.config.
    try:
        cfg_file = {**_read_plugin_config(), **normalized}
        _write_plugin_config(cfg_file)
    except Exception as e:
        logger.warning(f"[group_cloud_storage] config persist failed: {e}")
    for key, val in normalized.items():
        if hasattr(s.config, "set"):
            s.config.set(key, val)
        else:
            try:
                s.config[key] = val
            except Exception:
                pass
    reload_required = sorted(
        k for k in saved if k in {
            "request_interval_ms", "managed_groups", "global_admin_qqs",
            "download_server_enabled", "download_server_host", "download_http_port", "download_ftp_port",
            "openlist_enabled", "openlist_base_url", "openlist_username",
            "openlist_password", "openlist_token",
        }
    )
    return json_response({"saved": saved, "reload_required": reload_required})
