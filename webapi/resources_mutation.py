"""Resource mutation and delivery handlers."""
from __future__ import annotations
import json
from pathlib import Path
from astrbot.api import logger
from astrbot.api.web import error_response, json_response
try:
    from astrbot.api.web import _request_var as _web_request_var, PluginRequest as _PluginRequest
except ImportError:
    _web_request_var = None
    _PluginRequest = None
try:
    from starlette.requests import Request as _StarletteRequest
except ImportError:
    _StarletteRequest = None
from commands.handlers import Services
from core.api_validate import ApiValidationError, json_body, pick, qi
from .webapi_base import _param

async def api_file_tags(s: Services) -> dict:
    """Overwrite resource tags (undo supported)."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid = payload.get("id")
    tags = payload.get("tags")
    if not isinstance(fid, int) or not isinstance(tags, list):
        return error_response("id(int) and tags(list) required", status_code=400)
    if any(not isinstance(t, str) or len(t) > 24 for t in tags):
        return error_response("tag must be string(<=24)", status_code=400)
    detail = await s.store.get_resource_any(fid)
    old_tags = []
    if detail:
        try:
            old_tags = json.loads(detail.get("tags") or "[]")
        except (TypeError, ValueError):
            old_tags = []
    if not isinstance(old_tags, list):
        old_tags = []
    await s.store.update_resource_tags(fid, tags)
    cleaned = sorted({t.strip() for t in tags if t.strip()})
    # Direct operation log entry (task_id=''); undo = snapshot restore (ops_last_for_resource)
    await s.queue.record_op(
        "",
        "tags",
        before={"group_id": group, "id": fid, "tags": old_tags},
        after={"group_id": group, "id": fid, "tags": cleaned},
    )
    return json_response({"id": fid, "tags": cleaned})


async def api_tagcloud(s: Services) -> dict:
    """Tag cloud aggregation: global tag -> count over active resources.

    Module isolation: `?kind=album|essence` aggregates only that module's tags
    (file tags use the default global cloud; album and essence keep independent
    tag clouds and do not share the unified tag semantics).
    """
    kind = await _param("kind", "")
    cloud = await s.store.tag_cloud(kind if kind in ("album", "essence") else None)
    return json_response({"tags": cloud})


async def api_file_delete(s: Services) -> dict:
    """Delete a group file (queued; progress via SSE)."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid = payload.get("id")
    if not isinstance(fid, int):
        return error_response("id required(int)", status_code=400)
    try:
        task_id = await s.ops.submit_delete(group, fid)
    except ValueError as e:
        return error_response(str(e), status_code=404)
    return json_response({"task_id": task_id})


async def api_file_convert_volumes(s: Services) -> dict:
    """Manual volume conversion of an existing cloud file.

    Strictly user-initiated: the automatic post-sync sweep was removed on
    purpose (converting re-uploads and deletes a cloud file, so it must
    never happen as a side effect of scanning). Uploads of over-threshold
    files keep their own mandatory built-in volume pipeline.
    """
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid = payload.get("id")
    if not isinstance(fid, int):
        return error_response("id(int) required", status_code=400)
    try:
        task_id = await s.ops.submit_convert_volumes(group, fid)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id})


async def api_file_replace_name(s: Services) -> dict:
    """Rename (download-reupload): fetch original -> reupload under the new name -> delete old."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid, name = payload.get("id"), payload.get("new_name")
    if (
        not isinstance(fid, int)
        or not isinstance(name, str)
        or not (0 < len(name) <= 80)
    ):
        return error_response("id(int) and new_name(1..80) required", status_code=400)
    try:
        task_id = await s.ops.submit_replace_name(group, fid, name.strip())
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id})


async def api_file_move(s: Services) -> dict:
    """Move a file into the given folder (local index operation)."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid, folder = payload.get("id"), payload.get("folder_id")
    if not isinstance(fid, int) or not folder:
        return error_response("id(int) and folder_id required", status_code=400)
    try:
        task_id = await s.ops.submit_move(group, fid, str(folder))
    except ValueError as e:
        return error_response(str(e), status_code=404)
    return json_response({"task_id": task_id})


async def api_file_uri(s: Services) -> dict:
    """Locate a resource by cloud:// URI (encodable reference; programmatic query surface)."""
    uri = await _param("uri", "")
    if not uri.startswith("cloud://"):
        return error_response("uri must start with cloud://", status_code=400)
    try:
        row = await s.store.get_by_uri(uri)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    if not row:
        return error_response("resource not found", status_code=404)
    return json_response(
        {
            "uri": uri,
            "id": row["id"],
            "group_id": row["group_id"],
            "type": row["type"],
            "name": row["name"],
            "size": row.get("size") or 0,
            "status": row.get("status"),
            "meta": row.get("meta"),
            "tags": json.loads(row["tags"]) if row.get("tags") else [],
        }
    )


async def api_file_link(s: Services) -> dict:
    """Copy the download direct link: resolve a fresh file_id on the fly -> QQ CDN
    direct link (safe to hand out externally; its expiry is decided by QQ).
    """
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    fid = qi(await _param("id", "0")) or 0
    if fid <= 0:
        return error_response("id required", status_code=400)
    try:
        url, name = await s.ops.direct_link(group, fid)
    except ValueError as e:
        # not-found -> 404, volume guard -> 400
        return error_response(str(e), status_code=404 if "not found" in str(e) else 400)
    except Exception as e:
        logger.warning(f"[webapi] 直链获取失败: {e}", exc_info=True)
        return error_response(
            "直链获取失败（上游可能暂时不可用，可改用「下载」/「转存到网盘」或本机下载服务）",
            502,
        )
    return json_response(
        {"url": url, "name": name, "note": "QQ 文件直链有时效性，请及时下载"}
    )


async def api_download_address(s: Services) -> dict:
    """Local download service address: external clients can pull cloud files
    directly through this host's endpoints.
    """
    if not s.dlserver or not s.dlserver.enabled:
        return error_response(
            "下载服务未开启：请在插件配置开启 download_server_enabled", status_code=400
        )
    group = await _param("group", "")
    rid = await _param("id", "")
    if not group or not str(rid).isdigit():
        return error_response("group and id required", status_code=400)
    detail = await s.store.get_resource_detail(
        group, int(rid)
    ) or await s.store.get_resource_any(int(rid))
    if not detail:
        return error_response(f"resource {rid} not found", status_code=404)
    info = {
        "http_url": s.dlserver.download_url(group, int(rid)),
        "note": "HTTP 直链式下载：单文件 302 至 QQ CDN，分卷/视频流式返回",
    }
    if s.dlserver.ftp_port > 0:
        ftp = s.dlserver.ftp_info()
        info["ftp"] = {
            **ftp,
            "path": f"/{group}/{detail.get('name') or rid}",
            "note": "FTP 虚拟目录 /<群号>/<文件名>，RETR 按需拉取",
        }
    if s.dlserver.smb_port > 0 and getattr(s.dlserver, "smb_available", False):
        info["smb"] = {
            **s.dlserver.smb_info(group, detail.get("name") or str(rid)),
            "note": "SMB 共享按需落盘，首次打开前会有短暂延迟",
        }
    return json_response(info)


async def api_folder_create(s: Services) -> dict:
    """Create a group file folder (backed by create_group_file_folder)."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    name = str(payload.get("name") or "").strip()
    parent_id = str(payload.get("parent_id") or "/").strip() or "/"
    if not (0 < len(name) <= 60):
        return error_response("name length 1..60", status_code=400)
    try:
        task_id = await s.ops.submit_create_folder(group, name, parent_id)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id, "group": group, "name": name, "parent_id": parent_id})


async def api_folder_delete(s: Services) -> dict:
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    folder_id = str(payload.get("folder_id") or "").strip()
    if not folder_id:
        return error_response("folder_id required", status_code=400)
    try:
        await s.api.delete_group_file_folder(group, folder_id)
    except Exception as e:
        logger.warning(f"[webapi] delete folder failed: {e}", exc_info=True)
        return error_response("delete folder failed", status_code=502)
    return json_response({"ok": True, "group": group, "folder_id": folder_id})


async def api_folder_rename(s: Services) -> dict:
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    folder_id = str(payload.get("folder_id") or "").strip()
    name = str(payload.get("name") or payload.get("new_folder_name") or "").strip()
    if not folder_id or not name:
        return error_response("folder_id and name required", status_code=400)
    try:
        await s.api.rename_group_file_folder(group, folder_id, name)
    except Exception as e:
        logger.warning(f"[webapi] rename folder failed: {e}", exc_info=True)
        return error_response("rename folder failed", status_code=502)
    return json_response({"ok": True, "group": group, "folder_id": folder_id, "name": name})


async def api_file_download(s: Services) -> dict | object:
    """Download: fetch a fresh group file direct link -> stream-proxy it
    (Page iframes are restricted and must go through bridge.download).
    """
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    fid = qi(await _param("id", "0")) or 0
    if fid <= 0:
        return error_response("id required", status_code=400)
    # Degraded download: volume resources with missing parts can still be
    # downloaded incompletely (the caller is informed via the header)
    allow_incomplete = (await _param("allow_incomplete", "0")) in ("1", "true", "yes")
    import httpx
    from fastapi.responses import StreamingResponse

    try:
        target, name = await s.ops.download_info(
            group, fid, allow_incomplete=allow_incomplete
        )
    except ValueError as e:
        # Global fallback: id missing in the given group -> locate it across groups
        # (rows in the unified management view may carry a stale group id)
        try:
            d2 = (
                await s.store.get_resource_detail("*", fid)
                if False
                else await s.store.get_resource_any(fid)
            )
        except Exception:
            d2 = None
        if d2:
            target, name = await s.ops.download_info(
                d2["group_id"], fid, allow_incomplete=allow_incomplete
            )
        else:
            return error_response(f"{e}（全局亦无 id={fid}）", status_code=404)
    # RFC5987 filename (headers are latin-1 only; non-ASCII/special chars must be percent-encoded)
    from urllib.parse import quote as _quote

    safe_name = (name or "download").replace('"', "_")
    ascii_file = "download"
    disp = (
        f"attachment; filename=\"{ascii_file}\"; filename*=UTF-8''{_quote(safe_name)}"
    )
    from pathlib import Path as _Path

    # Volume reassembly: local temp file -> direct file response; single file: streamed URL proxy
    if _Path(target).exists():
        from fastapi.responses import FileResponse

        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename="download",
            headers={"Content-Disposition": disp},
        )

    async def _body():
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", target) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        _body(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": disp},
    )


async def api_files_scan(s: Services) -> dict:
    """In-group file scan (strictly separate from the group-info scan):
    - mode=all: refresh in-group file lists of all managed groups + capacity sync
    - mode=range: given group_ids (default = first group with unknown capacity plus
      up to 2 groups ranked above it, via default_range_ids)
    """
    payload = await json_body()
    mode = pick(payload, "mode", default="all", enum=("all", "range"))
    if mode == "range":
        ids = (payload or {}).get("group_ids") or []
        if not isinstance(ids, list) or not ids:
            ids = await s.scan.default_range_ids()
            if not ids:
                return json_response(
                    {
                        "task_id": "",
                        "mode": "range",
                        "groups": 0,
                        "note": "所有群容量已知，无需范围扫描",
                    }
                )
        task_id = await s.queue.submit(
            "file_scan", target="*", payload={"mode": "range", "groups": ids}
        )
        return json_response({"task_id": task_id, "mode": "range", "groups": len(ids)})
    task_id = await s.queue.submit("file_scan", target="*", payload={"mode": "all"})
    return json_response({"task_id": task_id, "mode": "all"})


async def api_files_sync(s: Services) -> dict:
    """Manually trigger this group's cloud file scan (full sync, queued and rate-limited)."""
    group = await _param("group", "")
    if group:
        if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
            return error_response("group not managed", status_code=403)
        task_id = await s.queue.submit("sync", target=group)
        return json_response({"task_id": task_id, "groups": 1})
    # No group given -> reject: use files/scan (all/range) instead of an implicit full pull
    return error_response(
        "请使用 files/scan 进行文件扫描（mode=all 全量 / mode=range 范围）——"
        "避免无明确目标的隐式全量云端拉取",
        status_code=400,
    )


async def api_file_detail(s: Services) -> dict:
    """File detail (volume info/hashes included; Page row action "detail")."""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    fid = qi(await _param("id", "0")) or 0
    if fid <= 0:
        return error_response("id required", status_code=400)
    d = await s.store.get_resource_detail(group, fid)
    if not d:
        return error_response("not found", status_code=404)
    vols = (
        await s.store.list_volumes(d["resource_id"])
        if (d.get("meta") or {}).get("volumes")
        else []
    )
    meta = d.get("meta") or {}
    return json_response(
        {
            "id": d["id"],
            "name": d["name"],
            "size": d.get("size", 0),
            "uploader": d.get("uploader_name") or d.get("uploader_id"),
            "source_ref": d.get("source_ref"),
            "uri": f"cloud://{d.get('group_id')}/{d.get('type')}/{d['id']}",
            "busid": d.get("busid"),
            "folder": d.get("folder_name"),
            "status": d.get("status"),
            "created_at": d.get("created_at"),
            "indexed_at": d.get("indexed_at"),
            "sha256": meta.get("total_sha256"),
            "volumes": [
                {
                    "seq": v.seq,
                    "part": v.part_name,
                    "size": v.size,
                    "sha256": v.sha256,
                    "status": v.status,
                    "ref_ready": bool(v.source_ref),
                }
                for v in vols
            ],
        }
    )


async def _managed_items(s: Services, items: list, failed: list):
    """Yield (fid, gid) for each batch item that parses and belongs to a
    page-managed group; failures are appended to `failed` (pass a throwaway
    list for silent-skip semantics).
    """
    managed = s.config.get("managed_groups", [])
    for it in items:
        try:
            fid = pick(it, "id", cast=int, required=True)
            gid = pick(it, "group", required=True, empty_allowed=False)
        except ApiValidationError as e:
            failed.append(str(e))
            continue
        if not await s.scan.is_page_managed(gid, managed):
            failed.append(f"group {gid} not managed")
            continue
        yield fid, gid


async def api_files_batch_delete(s: Services) -> dict:
    """Batch delete files: items=[{id,group}] queued one by one; per-item failures
    do not block the rest.
    """
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    failed: list[str] = []
    submitted = 0
    async for fid, gid in _managed_items(s, items, failed):
        try:
            await s.ops.submit_delete(gid, fid)
            submitted += 1
        except ValueError as e:
            failed.append(f"id={fid}: {e}")
    return json_response({"submitted": submitted, "failed": failed})


async def api_files_batch_move(s: Services) -> dict:
    """Batch move files: items=[{id,group}] moved into the same folder."""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    folder_id = pick(payload, "folder_id", required=True, empty_allowed=False)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    failed: list[str] = []
    submitted = 0
    async for fid, gid in _managed_items(s, items, failed):
        try:
            await s.ops.submit_move(gid, fid, str(folder_id))
            submitted += 1
        except ValueError as e:
            failed.append(f"id={fid}: {e}")
    return json_response({"submitted": submitted, "failed": failed})


async def api_files_batch_tags(s: Services) -> dict:
    """Batch set tags: items=[{id,group}] + tags (direct write to the local index)."""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    tags = pick(payload, "tags", cast=list, required=True)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    if any(not isinstance(t, str) or len(t) > 24 for t in tags):
        return error_response("tag must be string(<=24)", status_code=400)
    clean = sorted({t.strip() for t in tags if t.strip()})
    skipped: list[str] = []
    ids: list[int] = []
    async for fid, gid in _managed_items(s, items, skipped):
        # Group-scope the write: the tag lands on the resource the item
        # claims, not on any row that happens to share the numeric id.
        d = await s.store.get_resource_detail(gid, fid)
        if not d:
            skipped.append(f"id={fid}: not found in {gid}")
            continue
        ids.append(fid)
    for fid in ids:
        await s.store.update_resource_tags(fid, clean)
    return json_response({"updated": len(ids), "tags": clean, "skipped": skipped})


async def api_files_links(s: Services) -> dict:
    """Batch copy download direct links: items=[{id,group}], limit 20
    (resolved live, not persisted).
    """
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    if not items or len(items) > 20:
        return error_response("items required (1..20)", status_code=400)
    links, errors = [], []
    async for fid, gid in _managed_items(s, items, errors):
        try:
            url, name = await s.ops.direct_link(gid, fid)
            links.append({"id": fid, "name": name, "url": url})
        except Exception as e:
            logger.warning(f"[webapi] direct_link id={fid}: {e}", exc_info=True)
            errors.append(f"id={fid}: link failed")
    return json_response({"links": links, "errors": errors})
