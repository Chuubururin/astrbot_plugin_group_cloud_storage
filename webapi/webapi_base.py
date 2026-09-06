"""Shared infrastructure for the Page backend APIs.

Provides constants, parameter reading, serialization, group caching,
convert_to normalization, registration collection (catalog/handler mapping),
and _Bound lazy binding. webapi.py and the webapi_ext / webapi_netdisk
modules depend on this module one-way (no circular imports).
"""

from __future__ import annotations

import re
import time as _time_mod

from astrbot.api.web import request
from commands.handlers import Services

PLUGIN_NAME = "astrbot_plugin_group_cloud_storage"

# SSE heartbeat interval
SSE_HEARTBEAT_SEC = 30.0

# Cloud media fetch timeout for albums (seconds)
CLOUD_MEDIA_TIMEOUT = 12.0

# Extension whitelist for album image mode (aligned with cloud_ingest._IMAGE_EXTS)
_ALBUM_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _is_image_name(name: str) -> bool:
    """Whether the name has an image extension allowed by album image mode."""
    from pathlib import Path

    return Path(name or "").suffix.lower() in _ALBUM_IMAGE_EXTS


async def _param(key: str, default: str = "") -> str:
    """Unified parameter read: query first, then JSON body (bridge apiPost
    cannot carry a query)."""
    v = request.query.get(key, None, type=str)
    if v is not None:
        return str(v)
    try:
        body = await request.json(default={})
        if isinstance(body, dict) and key in body:
            return str(body[key])
    except Exception:
        pass
    return default


async def _ensure_ready(s: Services) -> None:
    """Lazy init: ensure the store/queue are ready on the first Page call
    (main injects the ready callback)."""
    if s.ready is not None:
        await s.ready()


_TAG_RE = re.compile(r"^[\w\u4e00-\u9fa5\-_]{0,32}$")


def _group_item(g) -> dict:
    """Serialize a group row (shared by groups and groups/removed)."""
    return {
        "group_id": g.group_id,
        "group_name": g.group_name,
        "display_name": g.display_name,
        "shown_name": g.shown_name,
        "role": g.role,
        "label": g.label,
        "sort_order": g.sort_order,
        "last_scan_at": g.last_scan_at,
        "used_space": g.used_space,
        "total_space": g.total_space,
        "file_count": g.file_count,
        "limit_count": getattr(g, "limit_count", 0) or 0,
        "managed": getattr(g, "managed", 1),
        "account_id": getattr(g, "account_id", "") or "",
        "album_count": g.album_count,
        "essence_count": g.essence_count,
        "file_type": None,
    }


# Managed-group list cache: search paths hit this cache instead of fetching
# all groups on every keystroke (important at large group counts)
_GROUP_LIST_CACHE: dict = {"key": None, "at": 0.0, "groups": None}


async def _managed_groups_cached(s: Services):
    """Cached managed-group list: returns only groups with managed=1."""
    key = tuple(s.config.get("managed_groups", []) or [])
    now = _time_mod.monotonic()
    if _GROUP_LIST_CACHE["key"] == key and now - _GROUP_LIST_CACHE["at"] < 30.0:
        return _GROUP_LIST_CACHE["groups"]
    groups = await s.scan.list_page_groups(s.config.get("managed_groups", []))
    _GROUP_LIST_CACHE.update(key=key, at=now, groups=groups)
    return groups


# convert_to whitelist (shared by fetch / netdisk distribute / upload prepare;
# dotted extensions)
_CONVERT_TO_EXT = {".mp4", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}


def _normalize_convert_to(value) -> str:
    """Normalize convert_to against the whitelist: valid values return a
    dotted extension ('.mp4'); empty or invalid values return ''."""
    v = str(value or "").strip().lstrip(".").lower()
    ext = f".{v}" if v else ""
    return ext if ext in _CONVERT_TO_EXT else ""


# Compatibility exports retained for older imports.
from .compatibility import Bound as _Bound
