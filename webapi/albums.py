"""Domain: Album/essence handlers."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body, pick
from .webapi_base import (
    CLOUD_MEDIA_TIMEOUT,
    _account_scope_for,
    _ensure_ready,
    _group_open_error,
    _param,
)


async def _refresh_album_id(s: Services, group: str, row: dict | None, album_id: str) -> str:
    """Re-list albums live and return a corrected album ID, or "" for none.

    A failed media fetch with an intact QQ session usually means the locally
    stored album ID is stale (the album was recreated in QQ). The fresh ID is
    matched by the album name of the clicked resource row and persisted into
    that row's meta. No destructive reconciliation runs here: the album scan
    owns the full listing sync.
    """
    try:
        albums = await asyncio.wait_for(
            s.api.get_group_album_list(group), timeout=CLOUD_MEDIA_TIMEOUT
        )
    except Exception:
        return ""
    live = {str(a.get("album_id") or "") for a in albums or []}
    if album_id in live:
        return ""  # stored ID still valid: transient failure, nothing to correct
    want = str((row or {}).get("name") or "")
    if not want:
        return ""
    for a in albums or []:
        if str(a.get("name") or a.get("album_name") or "") != want:
            continue
        fresh = str(a.get("album_id") or "")
        if fresh and row and row.get("id"):
            meta = dict(row.get("meta") or {})
            meta["album_id"] = fresh
            try:
                await s.store.update_resource_fields(
                    int(row["id"]), meta=json.dumps(meta, ensure_ascii=False)
                )
            except Exception:
                pass
        return fresh
    return ""


async def api_album_media(s: Services) -> dict:
    """Fetch album media live (cloud is the source: nothing persisted, display on demand)."""
    group = await _param("group", "")
    album_id = await _param("album_id", "")
    rid = await _param("id", "")
    # The album view lists without a group filter, so the page may omit the
    # group: fall back to the group of the resource row it clicked on.
    row = None
    if str(rid).isdigit():
        row = await s.store.get_resource_any(int(rid))
    if not group and row:
        group = str(row.get("group_id") or "")
    if err := await _group_open_error(s, group):
        return err
    if not album_id:
        # A numeric resource id resolves the real album ID from local meta; a
        # non-numeric value is the album ID itself (album IDs assigned by QQ
        # are alphanumeric and may contain * ! .).
        if row:
            album_id = str((row.get("meta") or {}).get("album_id") or "")
        else:
            album_id = str(rid or "")
    if not album_id:
        return error_response("album_id required", status_code=400)
    # Album OneBot calls run under the group owning account (single-account
    # routing; see _account_scope_for).
    async with _account_scope_for(s, group):
        try:
            media = await asyncio.wait_for(
                s.api.get_group_album_media_list(group, album_id),
                timeout=CLOUD_MEDIA_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return error_response(
                "云端相册媒体拉取超时（QQ 会话退化或网络波动），请稍后重试", status_code=504
            )
        except Exception:
            # One self-heal attempt: refresh the album ID from the live list
            # (stale meta) and retry once with the corrected or existing ID.
            fresh = await _refresh_album_id(s, group, row, album_id)
            retry_id = fresh or album_id
            try:
                media = await asyncio.wait_for(
                    s.api.get_group_album_media_list(group, retry_id),
                    timeout=CLOUD_MEDIA_TIMEOUT,
                )
            except Exception as e2:
                logger.warning(f"[webapi] album media unavailable: {e2}", exc_info=True)
                return error_response("album media unavailable", status_code=502)
            album_id = retry_id
        else:
            # A stale album ID yields an empty list instead of an error, so an
            # empty result with a clickable row also runs the refresh; the helper
            # only corrects an ID that vanished from the live list, so a genuinely
            # empty album keeps its single media fetch.
            fresh = await _refresh_album_id(s, group, row, album_id) if row else ""
            if fresh:
                try:
                    media = await asyncio.wait_for(
                        s.api.get_group_album_media_list(group, fresh),
                        timeout=CLOUD_MEDIA_TIMEOUT,
                    )
                except Exception:
                    media = []
                album_id = fresh
    return json_response({"album_id": album_id, "count": len(media), "media": media})


async def api_album_detail(s: Services) -> dict:
    """Album detail: stored resource row merged with the live album entry.

    The live list supplies the current media count (the stored upload_number
    is only a scan-time snapshot); a stale stored album ID is corrected the
    same way as in the media fetch.
    """
    rid = await _param("id", "")
    if not str(rid).isdigit():
        return error_response("id required", status_code=400)
    row = await s.store.get_resource_any(int(rid))
    if not row or str(row.get("type") or "") != "album":
        return error_response("album not found", status_code=404)
    group = await _param("group", "") or str(row.get("group_id") or "")
    if err := await _group_open_error(s, group):
        return err
    meta = row.get("meta") or {}
    album_id = str(meta.get("album_id") or "")
    live = None
    if album_id:
        async with _account_scope_for(s, group):
            try:
                albums = await asyncio.wait_for(
                    s.api.get_group_album_list(group), timeout=CLOUD_MEDIA_TIMEOUT
                )
            except Exception:
                albums = []
            live = next(
                (a for a in albums or [] if str(a.get("album_id") or "") == album_id),
                None,
            )
            if live is None and albums:
                fresh = await _refresh_album_id(s, group, row, album_id)
                if fresh:
                    album_id = fresh
                    live = next(
                        (a for a in albums if str(a.get("album_id") or "") == fresh),
                        None,
                    )
    cover = str(meta.get("cover_url") or "")
    if not cover and isinstance(live, dict):
        c = live.get("cover")
        cover = c.get("url") if isinstance(c, dict) else str(c or "")
    return json_response(
        {
            "id": row.get("id"),
            "name": str(row.get("name") or ""),
            "group_id": group,
            "album_id": album_id,
            "desc": str(meta.get("desc") or ""),
            "media_count": live.get("picNum") if live else meta.get("upload_number"),
            "cover_url": cover,
            "uploader": str(row.get("uploader_name") or row.get("uploader_id") or ""),
            "created_at": row.get("created_at"),
        }
    )


async def api_album_media_delete(s: Services) -> dict:
    """Delete group album media (protocol extension del_group_album_media; lloc = one media ID)."""
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
    payload = await json_body()
    album_id = str(payload.get("album_id") or "")
    lloc = str(payload.get("lloc") or "")
    if not album_id or not lloc:
        return error_response("album_id and lloc required", status_code=400)
    async with _account_scope_for(s, group):
        try:
            await s.api.delete_group_album_media(group, album_id, lloc)
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in ("unsupported", "not found", "404", "无此接口", "不支持")):
                return error_response(
                    "协议端（SnowLuma/NapCat）未实现「删除相册媒体」扩展接口", status_code=501
                )
            logger.warning(f"[webapi] delete album media failed: {e}", exc_info=True)
            return error_response("delete album media failed", status_code=502)
    return json_response({"status": "ok", "album_id": album_id, "lloc": lloc})


async def api_album_media_comment(s: Services) -> dict:
    """Post a group album media comment (protocol extension do_group_album_comment)."""
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
    payload = await json_body()
    album_id = str(payload.get("album_id") or "")
    lloc = str(payload.get("lloc") or "")
    content = str(payload.get("content") or "").strip()
    if not album_id or not lloc or not content:
        return error_response("album_id, lloc, content required", status_code=400)
    async with _account_scope_for(s, group):
        try:
            await s.api.comment_group_album_media(group, album_id, lloc, content)
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in ("unsupported", "not found", "404", "无此接口", "不支持")):
                return error_response(
                    "协议端（SnowLuma/NapCat）未实现「相册评论」扩展接口", status_code=501
                )
            logger.warning(f"[webapi] album comment failed: {e}", exc_info=True)
            return error_response("album comment failed", status_code=502)
    return json_response({"status": "ok", "album_id": album_id, "lloc": lloc})


async def api_album_create(s: Services) -> dict:
    """Create a group album through the optional OneBot extension template."""
    await _ensure_ready(s)
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
    payload = await json_body()
    name = str(payload.get("album_name") or "").strip()
    desc = str(payload.get("album_desc") or "").strip()
    if not name:
        return error_response("album_name required", status_code=400)
    async with _account_scope_for(s, group):
        try:
            result = await s.api.create_group_album(group, name, desc)
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in ("unsupported", "not found", "404", "无此接口", "不支持")):
                return error_response(
                    "协议端（SnowLuma/NapCat）未实现「创建相册」扩展接口："
                    "请在 QQ 客户端中手动创建相册后刷新本页",
                    status_code=501,
                )
            logger.warning(f"[webapi] create album failed: {e}", exc_info=True)
            return error_response("create album failed", status_code=502)
    return json_response({"group": group, "album_name": name, "album_desc": desc, "result": result})


async def api_album_video_preview(s: Services) -> dict:
    """Album video keyframe GIF preview: inlined as base64."""
    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    album_id = pick(payload, "album_id", required=True, empty_allowed=False)
    name = pick(payload, "name", default="")
    if err := await _group_open_error(s, group):
        return err
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    async with _account_scope_for(s, group):
        try:
            out = await s.ingest.video_preview_gif(group, album_id, name)
        except TimeoutError as e:
            return error_response(str(e), status_code=504)
        except ValueError as e:
            return error_response(str(e), status_code=404)
    return json_response(out)


async def api_essence_save(s: Services) -> dict:
    """Save essence text: long text is auto-split into chunks of at most 4500 characters."""
    group = await _param("group", "")
    if err := await _group_open_error(s, group):
        return err
    payload = await json_body()
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "")
    if not title or not text.strip():
        return error_response("title and text required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    async with _account_scope_for(s, group):
        try:
            task_id = await s.ingest.submit_essence_save(group, title, text)
        except ValueError as e:
            return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id, "group": group, "chars": len(text)})


async def api_essence_text(s: Services) -> dict:
    """Rebuild full essence text: reassemble from the cloud essence list via chunk markers."""
    group = await _param("group", "")
    rid = await _param("id", "")
    # Row-first group fallback: the essence view may list without a group
    # filter, so the page may omit the group.
    if not group and str(rid).isdigit():
        row = await s.store.get_resource_any(int(rid))
        group = str((row or {}).get("group_id") or "")
    if not group or not str(rid).isdigit():
        return error_response("group and id required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    # Open gate: the essence view may reach this handler without a prior
    # group-level check (row-click flows) — enforce the same gate here.
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        try:
            text, missing = await s.ingest.essence_full_text(group, int(rid))
        except TimeoutError as e:
            return error_response(str(e), status_code=504)
        except ValueError as e:
            return error_response(str(e), status_code=404)
    return json_response(
        {
            "group": group,
            "id": int(rid),
            "text": text,
            "missing_parts": missing,
            "complete": not missing,
        }
    )


async def api_essence_delete(s: Services) -> dict:
    """Delete essence: move each chunk out of the essence list -> soft-delete the resource."""
    group = await _param("group", "")
    rid = await _param("id", "")
    if not group or not str(rid).isdigit():
        return error_response("group and id required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    # Open gate: deletion mutates the group's essence list — enforce the same
    # gate as read paths (offline owner / dissolved group must not operate).
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        try:
            task_id = await s.ingest.submit_essence_delete(group, int(rid))
        except ValueError as e:
            return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id})
