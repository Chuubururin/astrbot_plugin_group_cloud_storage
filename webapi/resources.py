"""Domain: File/resource handlers."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.api.web import PluginUploadFile

try:  # Request-injection hook for unified invoke (invoke returns 501 when unavailable)
    from astrbot.api.web import _request_var as _web_request_var, PluginRequest as _PluginRequest
except ImportError:  # pragma: no cover
    _web_request_var = None
    _PluginRequest = None
try:
    from starlette.requests import Request as _StarletteRequest
except ImportError:  # pragma: no cover
    _StarletteRequest = None

from commands.handlers import Services
from core.api_validate import json_body
from core.domain.file_type import classify_with_overrides, type_exts_with_overrides
from core.domain.sync import ResourceQuery
from .webapi_base import (
    _param,
    _is_image_name,
    _managed_groups_cached,
    _group_open_error,
)


async def api_files(s: Services) -> dict:
    """File listing/search: group + q + type + page (Page read path)."""
    group = await _param("group", "")
    # Open gate: managed + owning account online + not dissolved (fail-closed).
    if err := await _group_open_error(s, group):
        return err
    q = await _param("q", "")
    ftype = await _param("type", "")
    kind = await _param("kind", "file")  # file/album/essence/all (unified resource catalog)
    page = max(1, request.query.get("page", 1, type=int))
    page_size = min(
        100,
        max(1, request.query.get("page_size", s.config.get("page_size", 10), type=int)),
    )
    sort_by = await _param("sort", "created_at")  # default: newest modified first
    sort_dir = await _param("order", "desc")
    if sort_by not in ("id", "name", "size", "created_at", "uploader_name"):
        sort_by = "created_at"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    folder = await _param("folder", "")
    account_id = await _param("account", "")  # account filter (file list filtered by account)
    # Status filter (netdisk / album / essence / none; derived, read-only, never written to DB)
    store_status = await _param("status", "")
    if store_status not in ("", "netdisk", "album", "essence", "none"):
        store_status = ""
    # Cross-group view: empty group aggregates managed groups of online
    # accounts by default (default rule: "all" = all online accounts' groups;
    # wither semantics keep offline accounts' groups out without deleting).
    # The account param picks one account's groups explicitly. Managed-group
    # list is cached for 30s (avoids refetching all groups on every keystroke)
    target_groups = None
    if not group:
        all_managed = await _managed_groups_cached(s, online_only=not account_id)
        if account_id:
            # Account filter: return only groups owned by this account
            target_groups = [
                g.group_id for g in all_managed if g.account_id == account_id
            ]
        else:
            target_groups = [g.group_id for g in all_managed]
    # #tag filter: #xxx tokens in the search string become a tags filter, the rest stays full-text
    q_tags: list[str] = []
    clean_q = q or ""
    if q:
        tokens = q.split()
        q_tags = [t[1:] for t in tokens if t.startswith("#") and len(t) > 1]
        clean_q = " ".join(t for t in tokens if not t.startswith("#"))
    ids = None
    if clean_q and s.searchkv is not None:
        # FTS5 on-disk search (no in-memory index warmup needed)
        ids = await s.searchkv.match_ids(group or None, clean_q)
    # type_ext_overrides config: classification and type filtering both use the
    # override-aware helpers (same source as the netdisk path)
    ext_overrides = s.config.get("type_ext_overrides") or {}
    rq_type = "file" if kind in ("file", "all") else kind
    # Status filter = cross-reference, never a type override: the Files tab
    # keeps type='file' rows and asks whether they also live in netdisk /
    # album / essence (SQL EXISTS subqueries in the store layer). Dropping the
    # type filter here would just mirror the other tabs' listings.
    rq = ResourceQuery(
        group_id=group or "",
        groups=target_groups,
        type=rq_type,
        keyword=clean_q or None,
        ids=ids,
        exts=(
            type_exts_with_overrides(ftype, ext_overrides)
            if (ftype and kind == "file")
            else None
        ),
        tags=q_tags or None,
        folder=folder if group else "",
        store_status=store_status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    result = await s.query.page_with(rq)
    # Folder dropdown source = the folders entity table (maintained by folder creation
    # and sync; resources.folder_name only covers folders that contain files)
    folders = (
        [
            {"id": f["folder_id"], "name": f["folder_name"]}
            for f in await s.store.list_folders_detail(group)
        ]
        if group
        else []
    )
    gmap = {}
    if target_groups:
        glist = await _managed_groups_cached(s)
        gmap = {g.group_id: (g.group_name or g.group_id) for g in glist}
    # Batch "in netdisk" determination for this page's resources (archive_map out+done)
    archived_ids: set[int] = set()
    if result.items:
        try:
            archived_ids = await s.store.list_archived_done_ids(
                [it.id for it in result.items], direction="out"
            )
        except Exception:
            archived_ids = set()
    # Batch cross-existence for this page's rows: same-group same-name
    # album/essence counterparts (the distribute pipelines keep the original
    # name on transfer, so name is the linkage channel per the requirement's
    # "文件名兜底" rule).
    album_copies: set[int] = set()
    essence_copies: set[int] = set()
    if result.items:
        try:
            copies = await s.store.find_cross_store_copies(
                [(it.id, it.group_id, it.name) for it in result.items]
            )
            album_copies = copies.get("album", set())
            essence_copies = copies.get("essence", set())
        except Exception:
            album_copies = essence_copies = set()
    # Volume completeness (missing volume -> "incomplete" status + partial download)
    vol_state: dict[int, dict] = {}
    for it in result.items:
        if not (it.meta or {}).get("volumes"):
            continue
        try:
            vols = await s.store.list_volumes(it.resource_id)
        except Exception:
            continue
        if not vols:
            continue
        done = sum(1 for v in vols if v.source_ref)
        vol_state[it.id] = {
            "volume_total": len(vols),
            "volume_done": done,
            "volume_complete": done == len(vols),
        }
    return json_response(
        {
            "items": [
                {
                    "id": it.id,
                    "name": it.name,
                    "size": it.size,
                    "uploader": it.uploader_name or it.uploader_id,
                    "favorite": None,
                    "modified": it.created_at,
                    "type": (
                        it.type
                        if it.type in ("album", "essence")
                        else classify_with_overrides(it.name, ext_overrides)
                    ),  # raw value (display mapping handled by the frontend dictionary)
                    "is_volume": bool((it.meta or {}).get("volumes")),
                    **(
                        {
                            "volume_total": vol_state[it.id]["volume_total"],
                            "volume_done": vol_state[it.id]["volume_done"],
                            "volume_complete": vol_state[it.id]["volume_complete"],
                        }
                        if it.id in vol_state
                        else {}
                    ),
                    # Long-video/long-text collection semantics (meta.parts has >1 segment)
                    "is_long": bool(
                        (it.meta or {}).get("parts")
                        and len((it.meta or {}).get("parts") or []) > 1
                    ),
                    "folder": it.folder_name,
                    "status": "active",
                    # Derived status (read-only projection, cross-reference
                    # semantics): netdisk = archived out+done; album/essence =
                    # a same-group same-name counterpart exists; else none.
                    "store_status": (
                        "netdisk"
                        if it.id in archived_ids
                        else "album"
                        if it.id in album_copies
                        else "essence"
                        if it.id in essence_copies
                        else "none"
                    ),
                    "group_id": it.group_id,
                    "group_name": gmap.get(it.group_id, ""),
                    "album_id": (it.meta or {}).get(
                        "album_id", ""
                    ),  # real album ID (used to view media)
                    "tags": list(it.tags or []),  # tags (information organization)
                    "uri": f"cloud://{it.group_id}/{it.type}/{it.id}",  # encodable reference
                    "path": it.path or "",  # logical path
                    "ext": it.ext or "",  # file extension
                }
                for it in result.items
            ],
            "total": result.total,
            "folders": folders,
            "page": result.page,
            "page_size": result.page_size,
            # Module-isolated tag cloud (album/essence independent; files use the global cloud)
            "tags": await s.store.tag_cloud(
                kind if kind in ("album", "essence") else None
            ),
        }
    )


# Storage capacity policy: default per-group total is 10GB (QQ official cap), used
# as the total fallback when fs is missing; missing used space falls back to the
# local index SUM (consistent with _capacity_of).
GROUP_TOTAL_DEFAULT = 10 * 1024 ** 3


def _aggregate_capacity(groups, local_sizes: dict[str, int]) -> tuple[int, int, int]:
    """Aggregate (used/total/group count): fs first; missing used -> local index,
    missing cap -> 10GB per group.
    """
    used_total = cap_total = 0
    for g in groups:
        used = g.used_space or local_sizes.get(g.group_id, 0)
        cap = g.total_space or GROUP_TOTAL_DEFAULT
        used_total += used
        cap_total += cap
    return used_total, cap_total, len(groups)


async def api_stat(s: Services) -> dict:
    """Statistics (file count/total size/capacity); empty group = global aggregate
    (unified management view, only groups with managed=1; default scope = online
    accounts' groups).

    ``accounts`` = operator accounts (QQ) of the stat scope: the owning account
    for a single group, or the distinct set of account_ids across the aggregated
    groups (the online set under the default scope). Each file operation is
    performed by exactly one account (the group's owning account); the list is
    for display only.
    """
    group = await _param("group", "")
    account_id = await _param("account", "")
    if err := await _group_open_error(s, group):
        return err
    if not group:
        groups = await _managed_groups_cached(s, online_only=not account_id)
        if account_id:
            groups = [g for g in groups if g.account_id == account_id]
        operator_accounts = sorted(
            {g.account_id for g in groups if getattr(g, "account_id", "")}
        )
        page = await s.query.page_with(
            ResourceQuery(groups=[g.group_id for g in groups], page_size=5000)
        )
        # Metadata accuracy: missing used -> local index fallback; missing cap -> 10GB per group
        local = {}
        for g in groups:
            if not g.used_space:
                try:
                    local[g.group_id] = await s.store.sum_resource_sizes(g.group_id)
                except Exception:
                    local[g.group_id] = 0
        used_total, cap_total, _ = _aggregate_capacity(groups, local)
        # Accurate total_size: sum file sizes across all groups in scope
        total_size = 0
        for g in groups:
            try:
                total_size += await s.store.sum_resource_sizes(g.group_id)
            except Exception:
                pass
        return json_response(
            {
                "group_id": "*",
                "file_count": page.total,
                "total_size": total_size,
                "uploaders": 0,
                "used_space": used_total,
                "total_space": cap_total,
                "accounts": operator_accounts,
            }
        )
    st = await s.stats.stats(group)
    # Operator of a single group = the account owning it (its bot performs the
    # group file operations); empty when the owning account is unrecorded.
    owning_account = ""
    try:
        for g in await s.store.list_groups():
            if g.group_id == group:
                owning_account = getattr(g, "account_id", "") or ""
                break
    except Exception:
        owning_account = ""
    return json_response(
        {
            "group_id": st.group_id,
            "file_count": st.file_count,
            "total_size": st.total_size,
            "uploaders": st.uploaders,
            "used_space": st.used_space,
            "total_space": st.total_space or GROUP_TOTAL_DEFAULT,
            "accounts": [owning_account] if owning_account else [],
        }
    )


# Upload is a two-step flow (the bridge endpoint does not allow query strings or
# special characters):
# 1) POST files/upload/prepare {group?, name} -> {token}
# 2) POST files/upload/<token> (multipart file field) performs the real upload
_UPLOAD_TOKENS: dict[str, dict] = {}
_UPLOAD_TOKEN_TTL = 600  # 10 minutes


def _cleanup_upload_tokens() -> None:
    """Remove expired upload tokens (lazy cleanup on each prepare call)."""
    now = time.time()
    expired = [k for k, v in _UPLOAD_TOKENS.items() if now - v.get("_ts", 0) > _UPLOAD_TOKEN_TTL]
    for k in expired:
        _UPLOAD_TOKENS.pop(k, None)


async def api_files_recommend_group(s: Services) -> dict:
    """Recommend an upload group (default rule when no group name/id is given).

    - kind=file: the group with the smallest id that has more free space than size;
    - kind=album|essence: the group with the smallest id (cap unknown, no capacity precheck);
    Returns {group_id, group_name, role, sort_order}; no candidate -> {"recommended": null}.
    """
    kind = await _param("kind", "file")
    if kind not in ("file", "album", "essence"):
        return error_response("kind must be file|album|essence", status_code=400)
    size = max(0, request.query.get("size", 0, type=int))
    rec = await s.ops.recommend_upload_group(kind=kind, requested_bytes=size)
    if rec is None:
        return json_response({"recommended": None})
    return json_response({"recommended": rec})


async def api_file_upload_prepare(s: Services) -> dict:
    """Register upload parameters and return a short-lived token.

    ``group`` may be empty: the default picker chooses the smallest group id
    with enough free space for the declared byte size (owner rule 4).
    ``name`` may be empty: the real multipart filename is used later.
    """
    payload = await json_body()
    group = str(payload.get("group") or "")
    # The bridge uploader sends "filename"; accept both spellings.
    name = str(payload.get("name") or payload.get("filename") or "")
    folder = str(payload.get("folder") or "")
    try:
        requested_bytes = max(0, int(payload.get("size") or 0))
    except (TypeError, ValueError):
        requested_bytes = 0
    if group:
        if err := await _group_open_error(s, group):
            return err
    else:
        # Default rule: smallest group id whose remaining space exceeds the file.
        rec = await s.ops.recommend_upload_group(
            kind="file", requested_bytes=requested_bytes
        )
        group = (rec or {}).get("group_id", "")
        if not group:
            return error_response("no managed group available", status_code=400)
    if name and not (0 < len(name) <= 80):
        return error_response("name length 1..80", status_code=400)
    mode = str(payload.get("mode") or "auto")
    if mode not in ("auto", "video", "text", "image"):
        return error_response("mode must be auto|video|text|image", status_code=400)
    to_album = bool(payload.get("to_album"))
    album_name = str(payload.get("album_name") or "AstrBot云盘").strip()
    # Upload format conversion (convert_to = target extension; empty = keep original)
    convert_to = str(payload.get("convert_to") or "").strip().lstrip(".")
    if convert_to and ("." + convert_to).lower() not in (
        ".mp4", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp",
    ):
        return error_response(
            "convert_to unsupported (video: mp4/mkv/webm; image: png/jpg/webp)",
            status_code=400,
        )
    convert_to = f".{convert_to}" if convert_to else ""
    if convert_to and mode == "text":
        return error_response(
            "convert_to is not applicable to text ingest", status_code=400
        )
    # Lossy album compression is the user's per-upload choice (checkbox +
    # quality tier); when unchecked the original file goes up untouched.
    lossy = bool(payload.get("lossy"))
    lossy_level = str(payload.get("lossy_level") or "medium").lower()
    if lossy_level not in ("high", "medium", "low"):
        lossy_level = "medium"
    # Full uuid4 hex = 128 bits of CSPRNG entropy (OWASP Session Management:
    # tokens should carry at least 128 bits to resist brute force). The token
    # is URL-safe hex and only ever compared by exact dict match.
    token = uuid4().hex
    _cleanup_upload_tokens()
    _UPLOAD_TOKENS[token] = {
        "_ts": time.time(),
        "group": group,
        "name": name,
        "folder": folder,
        "mode": mode,
        "to_album": to_album,
        "album_name": album_name,
        "convert_to": convert_to,
        "lossy": lossy,
        "lossy_level": lossy_level,
    }
    return json_response({"token": token, "group": group})


async def api_file_upload(s: Services, token: str) -> dict:
    """Upload a local file to a group (multipart field=file; group/name/folder come
    from the prepare token).

    Files larger than 95MB are delegated to the service layer for volume upload.
    """
    # Dynamic token routing: files/upload/<token> (token injected via the route keyword)
    meta = _UPLOAD_TOKENS.pop(token, None)
    if not meta:
        return error_response("upload token invalid/expired", status_code=400)
    group = meta["group"]
    name = meta["name"]
    folder = meta["folder"]
    files = await request.files()
    upload: PluginUploadFile | None = files.get("file")
    if not isinstance(upload, PluginUploadFile):
        return error_response("missing file field", status_code=400)
    # Stage into the data directory tmp/ (safe directory; filename reduced via basename)
    safe_name = Path(upload.filename or "").name or "unnamed"
    if name and not (0 < len(name) <= 80):
        return error_response("name length 1..80", status_code=400)
    target = s.ops.tmp_dir  # FileOpsService safe staging directory (data/plugin_data/.../tmp)
    if target is None:
        return error_response("upload tmp dir not configured", status_code=500)
    target.mkdir(parents=True, exist_ok=True)
    dest = target / f"{__import__('uuid').uuid4().hex[:12]}_{safe_name}"
    await upload.save(dest)
    dest_size = dest.stat().st_size
    # Format conversion (convert_to non-empty and mode != text; audio/documents unsupported)
    convert_to = str(meta.get("convert_to") or "")
    if convert_to and meta.get("mode") != "text":
        if s.converter is None:
            return error_response("converter not ready", status_code=500)
        try:
            original = dest
            dest = await s.converter.convert(dest, convert_to)
        except ValueError as e:
            return error_response(f"convert failed: {e}", status_code=400)
        if original != dest:
            original.unlink(missing_ok=True)  # remove the pre-conversion copy
        safe_name = f"{Path(safe_name).stem}{convert_to}"
        if name and Path(name).suffix.lower() != convert_to:
            name = f"{Path(name).stem}{convert_to}"
        dest_size = dest.stat().st_size
    # User-selected lossy album compression (checkbox + quality tier,
    # irreversible): images/videos are re-encoded before the album pipeline
    # only when the uploader opted in.
    if meta.get("to_album") and meta.get("lossy") and meta.get("mode") in ("video", "image"):
        if s.converter is None:
            return error_response("converter not ready", status_code=500)
        try:
            original = dest
            dest = await s.converter.compress(dest, meta.get("lossy_level") or "medium")
        except ValueError as e:
            return error_response(f"compress failed: {e}", status_code=400)
        if original != dest:
            original.unlink(missing_ok=True)
        safe_name = Path(safe_name).stem + dest.suffix
        if name and Path(name).suffix.lower() != dest.suffix.lower():
            name = f"{Path(name).stem}{dest.suffix}"
        dest_size = dest.stat().st_size
    # Large files (>95MB) go through volume upload (per-volume upload with
    # checksum reassembly; zip compression is mandatory and reversible)
    if dest_size > 95 * 1024 * 1024:
        task_id = await s.ops.submit_volume_upload(
            group,
            dest.as_posix(),
            (name or safe_name),
            folder or None,
        )
        return json_response(
            {"task_id": task_id, "staged": dest.name, "mode": "volumes"}
        )
    # mode=text -> import document text chunks as essence messages
    # (content read locally, not stored as a group file)
    if meta.get("mode") == "text":
        try:
            text = dest.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"[webapi] text read failed: {e}", exc_info=True)
            return error_response("text read failed", status_code=400)
        if len(text) > 2 * 1024 * 1024:
            return error_response("text file exceeds 2MB", status_code=400)
        dest.unlink(missing_ok=True)
        try:
            task_id = await s.ingest.submit_essence_save(
                group, (name or safe_name), text
            )
        except ValueError as e:
            return error_response(str(e), status_code=400)
        return json_response({"task_id": task_id, "staged": "", "mode": "text"})
    # mode=video -> long-video split storage (>600s auto-segmented, each segment <= 600s)
    if meta.get("mode") == "video":
        if meta.get("to_album"):
            # import media chunks into the group album
            task_id = await s.ingest.submit_video_album(
                group,
                dest.as_posix(),
                (name or safe_name),
                meta.get("album_name") or "AstrBot云盘",
            )
            return json_response(
                {"task_id": task_id, "staged": dest.name, "mode": "video_album"}
            )
        task_id = await s.ingest.submit_video_upload(
            group, dest.as_posix(), (name or safe_name), folder or None
        )
        return json_response({"task_id": task_id, "staged": dest.name, "mode": "video"})
    # mode=image -> import images into the group album (upload_image_to_qun_album)
    if meta.get("mode") == "image":
        if not _is_image_name(name or safe_name):
            return error_response(
                "image mode only accepts image extensions", status_code=400
            )
        task_id = await s.ingest.submit_image_album(
            group,
            dest.as_posix(),
            (name or safe_name),
            meta.get("album_name") or "AstrBot云盘",
        )
        return json_response({"task_id": task_id, "staged": dest.name, "mode": "image_album"})
    task_id = await s.ops.submit_upload(
        group, dest.as_posix(), (name or safe_name), folder or None
    )
    return json_response({"task_id": task_id, "staged": dest.name, "mode": "direct"})



