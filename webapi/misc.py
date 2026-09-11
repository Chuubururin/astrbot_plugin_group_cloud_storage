"""Domain: Preview/policy/fetch handlers — handlers extracted from webapi.py."""

from __future__ import annotations

from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body
from core.domain.file_type import (
    FILE_TYPE_EXT,
    FILE_TYPE_LABEL,
    classify_with_overrides,
    preview_policy_for,
)
from .webapi_base import (
    _group_open_error,
    _normalize_convert_to,
    _param,
)


async def api_preview_policy(s: Services) -> dict:
    """Preview policy lookup: ?ext=.mp4 -> {ext, type, mode, template}."""
    ext = (await _param("ext", "")).strip().lower()
    ext_overrides = s.config.get("type_ext_overrides") or {}
    ftype = classify_with_overrides(f"f{ext}", ext_overrides) if ext else "other"
    policy = preview_policy_for(ftype, s.config.get("preview_policy") or {})
    return json_response({"ext": ext, "type": ftype, **policy})


async def api_meta_classify(s: Services) -> dict:
    """Default classification tables (data-driven): source of the frontend
    type chips and local filtering."""
    return json_response({"ext_types": FILE_TYPE_EXT, "labels": FILE_TYPE_LABEL})


async def api_fetch(s: Services) -> dict:
    """Fetch an external URL into group files, a group album or essence.

    Albums accept images (direct upload) and videos (long-video sharding);
    an optional ``convert_to`` extension performs format conversion first.
    """
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
    payload = await json_body()
    url = str(payload.get("url") or "").strip()
    if not url:
        return error_response("url required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    convert_to = ""
    if payload.get("convert_to"):
        convert_to = _normalize_convert_to(payload.get("convert_to"))
        if not convert_to:
            return error_response(
                "convert_to unsupported (video: mp4/mkv/webm; image: png/jpg/webp)",
                status_code=400,
            )
        if payload.get("to_essence"):
            return error_response(
                "convert_to is not applicable to essence text ingest", status_code=400
            )
    try:
        # lossy/lossy_level: user-selected album compression (irreversible);
        # forwarded to submit_fetch which applies it on the album path
        # (previously dropped here, silently ignoring the user's choice).
        lossy_level = str(payload.get("lossy_level") or "medium").lower()
        if lossy_level not in ("high", "medium", "low"):
            lossy_level = "medium"
        task_id = await s.ingest.submit_fetch(
            group,
            url,
            name=str(payload.get("name") or "").strip(),
            to_album=bool(payload.get("to_album")),
            album_name=str(payload.get("album_name") or "").strip(),
            to_essence=bool(payload.get("to_essence")),
            convert_to=convert_to,
            lossy=bool(payload.get("lossy")),
            lossy_level=lossy_level,
        )
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response(
        {
            "task_id": task_id,
            "group": group,
            "to_album": bool(payload.get("to_album")),
            "to_essence": bool(payload.get("to_essence")),
        }
    )
