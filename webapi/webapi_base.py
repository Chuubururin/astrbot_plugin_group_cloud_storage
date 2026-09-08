"""Shared infrastructure for the Page backend APIs.

Provides constants, parameter reading, serialization, group caching,
convert_to normalization, registration collection (catalog/handler mapping),
and _Bound lazy binding. webapi.py and the webapi_ext / webapi_netdisk
modules depend on this module one-way (no circular imports).
"""

from __future__ import annotations

import re
import time as _time_mod
from contextlib import asynccontextmanager

from astrbot.api.web import error_response, request
from commands.handlers import Services

from core.opctx import account_scope

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


# Group open gate: a group targeted by a Page handler must pass managed +
# owning-account-online + dissolved(fail-closed) checks. Returns an
# error_response dict when the group must not be opened, None when open.
async def _group_open_error(s: Services, group: str) -> dict | None:
    """Unified open-gate for group-targeted handlers (403 wording distinguishes
    offline owner vs dissolved group; both keep the wither no-delete contract)."""
    if not group:
        return None
    try:
        await s.scan.assert_group_openable(
            group, s.config.get("managed_groups", [])
        )
    except ValueError as e:
        msg = str(e)
        if "离线" in msg:
            return error_response(msg, status_code=403)
        if "解散" in msg:
            return error_response(msg, status_code=403)
        return error_response("group not managed", status_code=403)
    except Exception:
        # Gate must never block on infrastructure hiccups beyond the explicit
        # checks above; the individual remote verify inside the gate already
        # fails closed, so any outer error is a local bug path.
        return error_response("group not managed", status_code=403)
    return None


@asynccontextmanager
async def _account_scope_for(s: Services, group: str):
    """account_scope of the group's owning account for direct s.api.* calls.

    Every single-file/group OneBot operation must execute under exactly one
    account (the group owner) — queue ops get this via OpDispatcher, but
    Page handlers that call s.api directly bypassed it and fell back to
    best_bot. Empty scope (unrecorded owner) keeps the best_bot fallback.
    """
    account_id = ""
    try:
        for g in await s.store.list_groups():
            if str(g.group_id) == str(group):
                account_id = str(getattr(g, "account_id", "") or "")
                break
    except Exception:
        account_id = ""
    with account_scope(account_id):
        yield


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


async def _managed_groups_cached(s: Services, online_only: bool = False):
    """Cached managed-group list: returns only groups with managed=1.

    online_only=True further restricts to groups whose owning account is
    currently online (default aggregation scope: wither semantics keep
    offline accounts' groups out of "all" views without deleting data).
    """
    online_ids = (
        frozenset(s.get_online_account_ids() if s.get_online_account_ids else ())
        if online_only
        else None
    )
    key = tuple(s.config.get("managed_groups", []) or []) + (
        ("online",) if online_only else ()
    )
    now = _time_mod.monotonic()
    if _GROUP_LIST_CACHE["key"] == key and now - _GROUP_LIST_CACHE["at"] < 30.0:
        return _GROUP_LIST_CACHE["groups"]
    groups = await s.scan.list_page_groups(s.config.get("managed_groups", []))
    if online_only:
        if online_ids:
            groups = [g for g in groups if not g.account_id or g.account_id in online_ids]
        else:
            # Unknown online set (no account callback wired): hide nothing.
            groups = [g for g in groups if not getattr(g, "account_id", "")]
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
from .compatibility import Bound as _Bound  # noqa: F401,E402  (re-export; must stay after the functions above)
