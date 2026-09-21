"""Page backend APIs — distribution/convert domain.

Hosts the four distribution handlers (files/albums/essence/netdisk
distribute). Route registration is NOT done here: webapi/routes.py is the
single route catalog, wired by webapi.register_page_apis.
"""

from __future__ import annotations

from .webapi_base import (
    _group_open_error,
    _normalize_convert_to,
    _param,
)
from astrbot.api import logger
from astrbot.api.web import error_response, json_response
from core.api_validate import json_body
from commands.handlers import Services


__all__ = [
    "api_files_distribute",
    "api_albums_distribute",
    "api_essence_distribute",
    "api_netdisk_distribute",
]


async def api_files_distribute(s: Services) -> dict:
    """Distribute a group file for download (target=local|netdisk|album|essence)."""
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
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
    except Exception as e:
        logger.warning(f"[webapi] distribute file failed: {e}", exc_info=True)
        return error_response("distribute failed", status_code=500)
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
    if err := await _group_open_error(s, group):
        return err
    if s.distributor is None:
        return error_response("distributor not ready", status_code=500)
    try:
        out = await s.distributor.distribute_album(group, album_id, name, target)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        logger.warning(f"[webapi] distribute album failed: {e}", exc_info=True)
        return error_response("distribute failed", status_code=500)
    return json_response(out)


async def api_essence_distribute(s: Services) -> dict:
    """Distribute essence full text for download (target=local|copy|netdisk|group)."""
    group = await _param("group", "")
    # Same open gate as the files/albums/netdisk distribute handlers: an
    # offline owning account or a dissolved group must not be able to submit a
    # transfer task (gate fails closed; wither semantics keep the data).
    if err := await _group_open_error(s, group):
        return err
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
    except Exception as e:
        logger.warning(f"[webapi] distribute essence failed: {e}", exc_info=True)
        return error_response("distribute failed", status_code=500)
    return json_response(out)


async def api_netdisk_distribute(s: Services) -> dict:
    """Distribute a netdisk file for download (target=local|group|album|essence).

    Accepts convert_to (whitelist-checked), supplied only in the request
    payload. Media is moved as-is: no lossy re-encode on distribute paths
    (re-encoding stays an explicit uploader opt-in at the panel upload).
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
    except Exception as e:
        logger.warning(f"[webapi] distribute netdisk failed: {e}", exc_info=True)
        return error_response("distribute failed", status_code=500)
    return json_response(out)