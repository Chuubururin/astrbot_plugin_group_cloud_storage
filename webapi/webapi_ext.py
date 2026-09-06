"""Page backend APIs — distribution/convert domain.

The four distribution endpoints (files/albums/essence/netdisk distribute);
imports the shared infrastructure from webapi_base.
"""

from __future__ import annotations

from .webapi_base import (
    PLUGIN_NAME,
    _Bound,
    _ensure_ready,
    _normalize_convert_to,
    _param,
)
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response
from core.api_validate import json_body
from commands.handlers import Services


__all__ = ["register_ext_apis"]


def register_ext_apis(context: Context, s: Services) -> None:
    """Register distribution/convert endpoints (catalog collection is wrapped
    centrally by webapi.register_page_apis)."""
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/distribute",
        _Bound(s, api_files_distribute),
        ["POST"],
        "文件下载分发（local/netdisk/album/essence）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/albums/distribute",
        _Bound(s, api_albums_distribute),
        ["POST"],
        "相册媒体下载分发（local/netdisk/group/essence）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/essence/distribute",
        _Bound(s, api_essence_distribute),
        ["POST"],
        "精华全文下载分发（local/copy/netdisk/group/album）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/netdisk/distribute",
        _Bound(s, api_netdisk_distribute),
        ["POST"],
        "网盘文件下载分发（local/group/album/essence）",
    )


async def api_files_distribute(s: Services) -> dict:
    """Distribute a group file for download (target=local|netdisk|album|essence)."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    rid = int(payload.get("id") or 0)
    target = str(payload.get("target") or "")
    if rid <= 0:
        return error_response("id required", status_code=400)
    if s.distributor is None:
        return error_response("distributor not ready", status_code=500)
    try:
        out = await s.distributor.distribute_file(group, rid, target)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response(out)


async def api_albums_distribute(s: Services) -> dict:
    """Distribute album media for download (target=local|netdisk|group)."""
    group = await _param("group", "")
    payload = await json_body()
    album_id = str(payload.get("album_id") or "")
    name = str(payload.get("name") or "")
    target = str(payload.get("target") or "")
    if not album_id:
        return error_response("album_id required", status_code=400)
    if group and not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    if s.distributor is None:
        return error_response("distributor not ready", status_code=500)
    try:
        out = await s.distributor.distribute_album(group, album_id, name, target)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response(out)


async def api_essence_distribute(s: Services) -> dict:
    """Distribute essence full text for download (target=local|copy|netdisk|group)."""
    group = await _param("group", "")
    payload = await json_body()
    rid = int(payload.get("id") or 0)
    target = str(payload.get("target") or "")
    if rid <= 0:
        return error_response("id required", status_code=400)
    if s.distributor is None:
        return error_response("distributor not ready", status_code=500)
    try:
        out = await s.distributor.distribute_essence(group, rid, target)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response(out)


async def api_netdisk_distribute(s: Services) -> dict:
    """Distribute a netdisk file for download (target=local|group|album|essence).

    Accepts convert_to (whitelist-checked), supplied only in the request
    payload. Album media is always lossy re-encoded (mandatory built-in).
    """
    payload = await json_body()
    path = str(payload.get("path") or "")
    target = str(payload.get("target") or "")
    group_id = str(payload.get("group") or "")
    name = str(payload.get("name") or "")
    convert_to = ""
    if payload.get("convert_to"):
        convert_to = _normalize_convert_to(payload.get("convert_to"))
        if not convert_to:
            return error_response(
                "convert_to unsupported (video: mp4/mkv/webm; image: png/jpg/webp)",
                status_code=400,
            )
    if not path:
        return error_response("path required", status_code=400)
    if s.distributor is None:
        return error_response("distributor not ready", status_code=500)
    try:
        out = await s.distributor.distribute_netdisk(
            path,
            target,
            group_id=group_id,
            name=name,
            convert_to=convert_to,
        )
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response(out)