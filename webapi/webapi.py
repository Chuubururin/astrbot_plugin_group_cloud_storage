"""Page 后端 API（docs/09 §5、§12.6）——薄壳：参数校验 → 鉴权 → 服务。

注册：main.py 调用 register_page_apis(context, services)。
约定：外部操作（扫描/真实改名）经 OpQueue（限速/重试/SSE）；本地字段修改直接写库。
"""

from __future__ import annotations

import asyncio
import json
import re

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request, stream_response

from pathlib import Path

from astrbot.api.web import PluginUploadFile
try:  # 4.27+：invoke 统一入口所需的 request 注入钩子（降级时 invoke 返回 501）
    from astrbot.api.web import _request_var as _web_request_var, PluginRequest as _PluginRequest
except ImportError:  # pragma: no cover
    _web_request_var = None
    _PluginRequest = None
try:
    from starlette.requests import Request as _StarletteRequest
except ImportError:  # pragma: no cover
    _StarletteRequest = None
from uuid import uuid4

from commands.handlers import Services
from .webapi_base import (
    PLUGIN_NAME,
    _Bound,
    _ensure_ready,
    _managed_groups_cached,
    _normalize_convert_to,
    _param,
    _TAG_RE,
    _group_item,
    _is_image_name,
    _CONVERT_TO_EXT,
    capture_all,
)
from .webapi_ext import register_ext_apis
from .webapi_netdisk import register_netdisk_apis
from core.api_validate import ApiValidationError, json_body, pick, qi
from core.domain.file_type import (
    FILE_TYPE_EXT,
    FILE_TYPE_LABEL,
    classify,
    classify_with_overrides,
    preview_policy_for,
    type_exts,
)
from core.domain.sync import ResourceQuery

PLUGIN_NAME = "astrbot_plugin_group_cloud_storage"

# SSE 心跳间隔（秒，CT-6 心跳保持；前端 constants.SSE_HEARTBEAT_TIMEOUT_MS 以 3 倍判定）
SSE_HEARTBEAT_SEC = 30.0

# 相册媒体云端拉取超时（秒）
CLOUD_MEDIA_TIMEOUT = 12.0

# 2026-09-01 N-06：相册 image 模式的扩展名白名单（与 cloud_ingest._IMAGE_EXTS 对齐）
_ALBUM_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


async def api_groups(s: Services) -> dict:
    """群清单（Page 可管理：白名单 ∪ owned）+ 扫描状态 + 队列状态。

    仅显示 managed=1 的群（离线账号的群在扫描时被标记为 managed=0）。
    """
    groups = await s.scan.list_page_groups(s.config.get("managed_groups", []))
    capacity = s.planner.capacity_stats(groups) if s.planner else {}
    if capacity:
        capacity["album_count"] = sum((g.album_count or 0) for g in groups)
        capacity["essence_count"] = sum((g.essence_count or 0) for g in groups)
    return json_response(
        {
            "capacity": capacity,
            "groups": [_group_item(g) for g in groups],
            "scan": (s.scan.last_result.as_dict() if s.scan.last_result else None),
            "queue": await s.queue.status(),
            "managed_groups": s.config.get("managed_groups", []),
        }
    )


async def api_scan(s: Services) -> dict:
    """扫描双模式：
    - 全量（默认）：全部群（群信息+容量+新群判定）
    - 范围：body {mode:"range", group_ids?:[...]}；未指定群时按排序取
      「已用容量为 - 的群」及其上方 2 个（default_range_ids）。
    """
    payload = await json_body()
    mode = pick(
        payload, "mode", default="incremental", enum=("all", "incremental", "range")
    )  # 增量优先原则
    if mode in ("all", "incremental"):
        task_id = await s.queue.submit("scan", target="*", payload={"mode": mode})
        return json_response({"task_id": task_id, "mode": mode})
    ids = (payload or {}).get("group_ids")
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
    # 语义：范围=群内文件扫描（file_scan）；群信息无手动范围
    task_id = await s.queue.submit(
        "file_scan", target="*", payload={"mode": "range", "groups": ids}
    )
    return json_response({"task_id": task_id, "mode": "range", "groups": len(ids)})


async def api_groups_removed(s: Services) -> dict:
    """已移除管理的群（v2.7.1）：仅显示当前在线账号的已移除群。"""
    online_ids = s.get_online_account_ids() if s.get_online_account_ids else set()
    rows = [
        g
        for g in await s.store.list_groups()
        if getattr(g, "managed", 1) == 0
        and (not online_ids or g.account_id in online_ids)
    ]
    return json_response(
        {
            "groups": [_group_item(g) for g in rows],
            "managed_groups": s.config.get("managed_groups", []),
        }
    )


async def api_groups_restore(s: Services) -> dict:
    """恢复管理（v2.7.1）：managed=0 → 1，重新进入管理列表。"""
    payload = await json_body()
    ids = pick(payload, "group_ids", cast=list, required=True)
    if not ids or len(ids) > 500:
        return error_response("group_ids required (1..500)", status_code=400)
    gids = [str(x) for x in ids if str(x)]
    await s.store.set_groups_managed(gids, 1)
    return json_response({"restored": len(gids)})


async def api_accounts(s: Services) -> dict:
    """账号清单（v2.11）：统一/单独管理——仅在线账号与名下群数量。"""
    accounts = await s.store.list_accounts()
    # 获取在线账号 ID 集合
    online_ids = s.get_online_account_ids() if s.get_online_account_ids else set()
    # 只保留在线账号（离线账号的群已被标记为 managed=0）
    online_accounts = []
    for a in accounts:
        if a["account_id"] in online_ids:
            a["online"] = True
            online_accounts.append(a)
    total_groups = sum(a["groups"] for a in online_accounts)
    return json_response(
        {
            "accounts": online_accounts,
            "total_groups": total_groups,
            "online_count": len(online_accounts),
        }
    )


async def api_groups_batch_actions(s: Services) -> dict:
    """批量群操作（v1.4）：改名 / 加群方式 / 备注 —— 入队逐群真实调用。"""
    payload = await json_body()
    group_ids = payload.get("group_ids")
    action = str(payload.get("action") or "")
    value = payload.get("value")
    if not isinstance(group_ids, list) or not group_ids:
        return error_response("group_ids required", status_code=400)
    if action not in ("rename", "add_option", "remark"):
        return error_response(
            "action must be rename|add_option|remark", status_code=400
        )
    if action == "rename":
        if not isinstance(value, str) or not (0 < len(value) <= 60):
            return error_response("rename value length 1..60", status_code=400)
    if action == "remark":
        if not isinstance(value, str) or not (0 < len(value) <= 60):
            return error_response("remark value length 1..60", status_code=400)
    if action == "add_option":
        if not isinstance(value, int) or value not in (1, 2, 3, 4, 5):
            return error_response("add_option value must be int 1..5", status_code=400)
    managed = s.config.get("managed_groups", [])
    for gid in group_ids:
        if not gid or not await s.scan.is_page_managed(str(gid), managed):
            return error_response(f"group {gid} not managed", 403)
    task_id = await s.queue.submit(
        "batch_groups",
        target="*",
        payload={
            "action": action,
            "value": value,
            "group_ids": [str(g) for g in group_ids],
        },
    )
    return json_response({"task_id": task_id, "groups": len(group_ids)})


async def api_groups_batch_update(s: Services) -> dict:
    """批量改名/标号：body = [{group_id, display_name?, label?, set_remote?}]。"""
    payload = await json_body()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return error_response("items required", status_code=400)
    managed = s.config.get("managed_groups", [])
    applied, queued = 0, 0
    for it in items:
        gid = str(it.get("group_id") or "")
        if not gid or not await s.scan.is_page_managed(gid, managed):
            return error_response(f"group {gid} not managed", 403)
        display = it.get("display_name")
        label = it.get("label")
        if display is not None and not (0 < len(str(display)) <= 80):
            return error_response("display_name length 1..80", status_code=400)
        if label is not None and not _TAG_RE.match(str(label)):
            return error_response("label invalid", status_code=400)
        # set_remote 默认 true：真实改名（调用 set_group_name）为主动作，
        # 本地 display_name/label 仅在校验通过（群名一致）后回填 —— 未校验不填充；
        # set_remote=false：纯本地字段即时写。
        if it.get("set_remote", True) and display:
            await s.queue.submit(
                "rename",
                target=gid,
                payload={
                    "name": str(display),
                    "display_name": str(display),
                    "label": it.get("label"),
                },
            )
            queued += 1
        elif display is not None or label is not None:
            fields = {}
            if display is not None:
                fields["display_name"] = str(display)
            if label is not None:
                fields["label"] = str(label)
            await s.store.update_group_fields(gid, **fields)
            applied += 1
    return json_response({"applied": applied, "queued": queued})


async def api_groups_order(s: Services) -> dict:
    """排序持久化：body = {ordered_ids: [...]}。"""
    payload = await json_body()
    ordered = payload.get("ordered_ids")
    if not isinstance(ordered, list):
        return error_response("ordered_ids required", status_code=400)
    ids = [str(x) for x in ordered if str(x)]
    await s.store.reorder_groups(ids)
    return json_response({"ok": True, "count": len(ids)})


async def api_groups_remove(s: Services) -> dict:
    """移除被管理条目（managed=0，列表隐藏且扫描不复活；不删除真实群）。"""
    payload = await json_body()
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return error_response("items required", status_code=400)
    ids = [str(x) for x in items if str(x)]
    await s.store.set_groups_managed(ids, 0)
    return json_response({"removed": len(ids)})


async def api_files(s: Services) -> dict:
    """文件列表/检索：group + q + type + page（Page 读路径，P1）。"""
    group = await _param("group", "")
    managed = s.config.get("managed_groups", [])
    if group and not await s.scan.is_page_managed(group, managed):
        return error_response("group not managed", status_code=403)
    q = await _param("q", "")
    ftype = await _param("type", "")
    kind = await _param("kind", "file")  # file/album/essence/all（v9 统一资源目录）
    page = max(1, request.query.get("page", 1, type=int))
    page_size = min(
        100,
        max(1, request.query.get("page_size", s.config.get("page_size", 10), type=int)),
    )
    sort_by = await _param("sort", "created_at")  # 2026-09-01 N-07：缺省按修改时间新到旧
    sort_dir = await _param("order", "desc")
    if sort_by not in ("id", "name", "size", "created_at", "uploader_name"):
        sort_by = "created_at"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    folder = await _param("folder", "")
    account_id = await _param("account", "")  # v2.12：账号筛选（文件列表按账号过滤）
    # 2026-09-01 N-02：状态筛选（在网盘/在相册/在精华消息/未下载；派生，只读不写库）
    store_status = await _param("status", "")
    if store_status not in ("", "netdisk", "album", "essence", "none"):
        store_status = ""
    # 跨群统一视图：group 空 → 全部被管理群聚合（文件不受群约束）
    # v2.10：受管群清单 30s 缓存（每击键不再全量拉群）
    target_groups = None
    if not group:
        all_managed = await _managed_groups_cached(s)
        if account_id:
            # 按账号过滤：仅返回该账号的群
            target_groups = [
                g.group_id for g in all_managed if g.account_id == account_id
            ]
        else:
            target_groups = [g.group_id for g in all_managed]
    # v1.3：#标签 过滤（搜索词中的 #xxx 转 tags 过滤，其余继续全文）
    q_tags: list[str] = []
    clean_q = q or ""
    if q:
        tokens = q.split()
        q_tags = [t[1:] for t in tokens if t.startswith("#") and len(t) > 1]
        clean_q = " ".join(t for t in tokens if not t.startswith("#"))
    ids = None
    if clean_q and s.searchkv is not None:
        # v2.9：FTS5 磁盘检索（无需内存索引预热）
        ids = await s.searchkv.match_ids(group or None, clean_q)
    rq_type = "file" if kind in ("file", "all") else kind
    # 2026-09-01 N-02：状态筛选与类型段联动——album/essence/none 状态
    # 由 store_status 分支承担类型语义，不再叠加 type='file' 过滤（否则永不命中）
    if store_status in ("album", "essence", "none"):
        rq_type = None
    rq = ResourceQuery(
        group_id=group or "",
        groups=target_groups,
        type=rq_type,
        keyword=clean_q or None,
        ids=ids,
        exts=type_exts(ftype) if (ftype and kind == "file") else None,
        tags=q_tags or None,
        folder=folder if group else "",
        store_status=store_status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    result = await s.query.page_with(rq)
    # 目录下拉数据源 = folders 实体表（新建目录/同步维护；resources.folder_name 仅覆盖有文件的目录）
    folders = (
        [f["folder_name"] for f in await s.store.list_folders_detail(group)]
        if group
        else []
    )
    gmap = {}
    if target_groups:
        glist = await _managed_groups_cached(s)
        gmap = {g.group_id: (g.group_name or g.group_id) for g in glist}
    # 2026-09-01 N-02：本页资源「在网盘」批量判定（archive_map out+done）
    archived_ids: set[int] = set()
    if result.items:
        try:
            archived_ids = await s.store.list_archived_done_ids(
                [it.id for it in result.items], direction="out"
            )
        except Exception:
            archived_ids = set()
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
                        else classify(it.name)
                    ),  # CT-4/FE-8：机器值（v2.13.1，中文显示由前端字典承担）
                    "is_volume": bool((it.meta or {}).get("volumes")),
                    # 2026-09-01 N-10 集语义：长视频/长文本集（meta.parts 分片 >1 段）
                    "is_long": bool(
                        (it.meta or {}).get("parts")
                        and len((it.meta or {}).get("parts") or []) > 1
                    ),
                    "folder": it.folder_name,
                    "status": "active",
                    # 2026-09-01 N-02 派生状态：album/essence/netdisk/none（只读投影）
                    "store_status": (
                        it.type
                        if it.type in ("album", "essence")
                        else ("netdisk" if it.id in archived_ids else "none")
                    ),
                    "group_id": it.group_id,
                    "group_name": gmap.get(it.group_id, ""),
                    "album_id": (it.meta or {}).get(
                        "album_id", ""
                    ),  # 相册真实 ID（查看媒体用）
                    "tags": list(it.tags or []),  # v1.3 标签（信息整理）
                    "uri": f"cloud://{it.group_id}/{it.type}/{it.id}",  # v1.7 可编码化引用
                    "path": it.path or "",  # v1.7 逻辑路径
                    "ext": it.ext or "",  # v1.7 扩展名
                }
                for it in result.items
            ],
            "total": result.total,
            "folders": folders,
            "page": result.page,
            "page_size": result.page_size,
            # 2026-09-01 W-9：模块隔离标签云（相册/精华各自独立；文件=全局）
            "tags": await s.store.tag_cloud(
                kind if kind in ("album", "essence") else None
            ),
        }
    )


# 2026-09-03 存储信息口径（元数据准确性）：每群默认总容量 10GB（QQ 官方上限），
# fs 缺失时用于总量兜底；已用空间缺失时用本地索引 SUM 兜底（与 _capacity_of 一致）。
GROUP_TOTAL_DEFAULT = 10 * 1024 ** 3


def _aggregate_capacity(groups, local_sizes: dict[str, int]) -> tuple[int, int, int]:
    """聚合（已用/总容量/群数）：fs 优先；used 缺失→本地索引；cap 缺失→10GB/群。"""
    used_total = cap_total = 0
    for g in groups:
        used = g.used_space or local_sizes.get(g.group_id, 0)
        cap = g.total_space or GROUP_TOTAL_DEFAULT
        used_total += used
        cap_total += cap
    return used_total, cap_total, len(groups)


async def api_stat(s: Services) -> dict:
    """统计（文件数/总大小/容量）；group 空=全局聚合（统一管理视图，仅 managed=1 的群）。"""
    group = await _param("group", "")
    managed = s.config.get("managed_groups", [])
    if group and not await s.scan.is_page_managed(group, managed):
        return error_response("group not managed", status_code=403)
    if not group:
        groups = await s.scan.list_page_groups(managed)
        page = await s.query.page_with(
            ResourceQuery(groups=[g.group_id for g in groups], page_size=5000)
        )
        # 2026-09-03 元数据准确性：used 缺失→本地索引兜底；cap 缺失→10GB/群兜底
        local = {}
        for g in groups:
            if not g.used_space:
                try:
                    local[g.group_id] = await s.store.sum_resource_sizes(g.group_id)
                except Exception:
                    local[g.group_id] = 0
        used_total, cap_total, _ = _aggregate_capacity(groups, local)
        return json_response(
            {
                "group_id": "*",
                "file_count": page.total,
                "total_size": sum((it.size or 0) for it in page.items),
                "uploaders": 0,
                "used_space": used_total,
                "total_space": cap_total,
            }
        )
    st = await s.stats.stats(group)
    return json_response(
        {
            "group_id": st.group_id,
            "file_count": st.file_count,
            "total_size": st.total_size,
            "uploaders": st.uploaders,
            "used_space": st.used_space,
            "total_space": st.total_space or GROUP_TOTAL_DEFAULT,
        }
    )


# 上传分两步（bridge endpoint 不允许 query/特殊字符）：
# 1) POST files/upload/prepare {group?, name} -> {token}
# 2) POST files/upload/<token>（multipart file 字段）走真实上传
_UPLOAD_TOKENS: dict[str, dict] = {}


async def api_files_recommend_group(s: Services) -> dict:
    """2026-09-01 N-07：推荐上传群（缺省规则：未指定群名/群号时）。

    - kind=file：群号值最小同时剩余空间 > size 的群；
    - kind=album|essence：群号值最小的群（上限未知，不预检容量）；
    返回 {group_id, group_name, role, sort_order}；无候选 404 语义（400 亦可，按空处理）。
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
    # The bridge uploader historically sent "filename"; accept both spellings.
    name = str(payload.get("name") or payload.get("filename") or "")
    folder = str(payload.get("folder") or "")
    try:
        requested_bytes = max(0, int(payload.get("size") or 0))
    except (TypeError, ValueError):
        requested_bytes = 0
    if group:
        if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
            return error_response("group not managed", status_code=403)
    else:
        # N-07 rule 4: smallest group id whose remaining space exceeds the file.
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
    lossy = bool(payload.get("lossy"))
    compress = bool(payload.get("compress"))
    album_name = str(payload.get("album_name") or "AstrBot云盘").strip()
    # 2026-09-02 W2-B：上传格式转换（convert_to=目标扩展名；不传=原样）
    convert_to = str(payload.get("convert_to") or "").strip().lstrip(".")
    if convert_to and not ("." + convert_to).lower() in (
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
    token = uuid4().hex[:16]
    _UPLOAD_TOKENS[token] = {
        "group": group,
        "name": name,
        "folder": folder,
        "mode": mode,
        "to_album": to_album,
        "lossy": lossy,
        "compress": compress,
        "album_name": album_name,
        "convert_to": convert_to,
    }
    return json_response({"token": token, "group": group})


async def api_file_upload(s: Services, token: str) -> dict:
    """上传本地文件到群（multipart field=file；group/name/folder 走 query）。

    大文件（>95MB）分卷由服务层负责（P3a）；当前阶段单文件直接上传（≤95MB）。
    """
    # 动态 token 路由：files/upload/<token>（token 由路由关键字注入）
    meta = _UPLOAD_TOKENS.pop(token, None)
    if not meta:
        return error_response("upload token invalid/expired", status_code=400)
    group = meta["group"]
    name = meta["name"]
    folder = meta["folder"]
    form = await request.form()
    files = await request.files()
    upload: PluginUploadFile | None = files.get("file")
    if not isinstance(upload, PluginUploadFile):
        return error_response("missing file field", status_code=400)
    # 暂存到数据目录 tmp/（安全目录；文件名经 basename 白名单）
    safe_name = Path(upload.filename or "").name or "unnamed"
    if name and not (0 < len(name) <= 80):
        return error_response("name length 1..80", status_code=400)
    target = s.ops.tmp_dir  # FileOpsService 安全的暂存目录（data/plugin_data/.../tmp）
    if target is None:
        return error_response("upload tmp dir not configured", status_code=500)
    target.mkdir(parents=True, exist_ok=True)
    dest = target / f"{__import__('uuid').uuid4().hex[:12]}_{safe_name}"
    await upload.save(dest)
    dest_size = dest.stat().st_size
    # 2026-09-02 W2-B：格式转换（convert_to 非空且非 text 模式；音频/文档不在列）
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
    # Optional lossy album compression (C-4): images/videos are re-encoded
    # before the album pipeline and the original temporary copy is removed.
    if meta.get("lossy") and meta.get("mode") in ("video", "image"):
        if s.converter is None:
            return error_response("converter not ready", status_code=500)
        try:
            original = dest
            dest = await s.converter.compress(dest)
        except ValueError as e:
            return error_response(f"compress failed: {e}", status_code=400)
        if original != dest:
            original.unlink(missing_ok=True)
        safe_name = Path(safe_name).stem + dest.suffix
        if name and Path(name).suffix.lower() != dest.suffix.lower():
            name = f"{Path(name).stem}{dest.suffix}"
        dest_size = dest.stat().st_size
    # 大文件（>95MB）走分卷（WinRAR 模式：逐卷上传+校验重组，云端永久化）
    if dest_size > 95 * 1024 * 1024:
        task_id = await s.ops.submit_volume_upload(
            group,
            dest.as_posix(),
            (name or safe_name),
            folder or None,
            compress=bool(meta.get("compress")),
        )
        return json_response(
            {"task_id": task_id, "staged": dest.name, "mode": "volumes"}
        )
    # v2.8：mode=text → 文档文本分片导入精华（本地读取内容，不入群文件）
    if meta.get("mode") == "text":
        try:
            text = dest.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return error_response(f"text read failed: {e}", status_code=400)
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
    # v1.2：mode=video → 长视频拆分存储（>600s 自动分段，每段 ≤600s）
    if meta.get("mode") == "video":
        if meta.get("to_album"):
            # v2.8：媒体分片导入群相册
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
    # 2026-09-01 N-06：mode=image → 图片导入群相册（upload_image_to_qun_album）
    if meta.get("mode") == "image":
        if not _is_image_name(name or safe_name):
            return error_response(
                f"image mode only accepts image extensions", status_code=400
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


async def api_file_tags(s: Services) -> dict:
    """标签覆盖写入（v1.3 信息整理；v15 直连操作流记录，供标签撤销）。"""
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
    # v15：直连操作流（task_id=''），撤销=快照恢复（ops_last_for_resource）
    await s.queue.record_op(
        "",
        "tags",
        before={"group_id": group, "id": fid, "tags": old_tags},
        after={"group_id": group, "id": fid, "tags": cleaned},
    )
    return json_response({"id": fid, "tags": cleaned})


async def api_tagcloud(s: Services) -> dict:
    """标签云聚合（v1.3）：active 资源全局 tag → 计数。

    2026-09-01 W-9 分模块隔离：`?kind=album|essence` 仅聚合该模块标签
    （文件标签走默认全局；相册/精华各自独立标签云，不复用统一标签语义）。
    """
    kind = await _param("kind", "")
    cloud = await s.store.tag_cloud(kind if kind in ("album", "essence") else None)
    return json_response({"tags": cloud})


async def api_file_delete(s: Services) -> dict:
    """删除群文件（入队，SSE 进度）。"""
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
        return error_response(str(e), 404)
    return json_response({"task_id": task_id})


async def api_album_media(s: Services) -> dict:
    """相册媒体实时拉取（云端为源：不落库，仅按需展示）。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    album_id = await _param("album_id", "")
    if not album_id:
        # 兼容行内资源主键：从本地 meta 解析真实相册 ID
        rid = await _param("id", "")
        row = await s.store.get_resource_any(int(rid)) if str(rid).isdigit() else None
        album_id = (row or {}).get("meta", {}).get("album_id", "") if row else ""
    if not album_id:
        return error_response("album_id required", status_code=400)
    try:
        media = await asyncio.wait_for(
            s.api.get_group_album_media_list(group, album_id),
            timeout=CLOUD_MEDIA_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return error_response(
            "云端相册媒体拉取超时（QQ 会话退化或网络波动），请稍后重试", status_code=504
        )
    except Exception as e:
        return error_response(f"album media unavailable: {e}", status_code=502)
    return json_response({"album_id": album_id, "count": len(media), "media": media})


async def api_essence_save(s: Services) -> dict:
    """精华文本入库（v1.2）：长文本按 4500 字上限自动拆分存储。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "")
    if not title or not text.strip():
        return error_response("title and text required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    try:
        task_id = await s.ingest.submit_essence_save(group, title, text)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id, "group": group, "chars": len(text)})


async def api_essence_text(s: Services) -> dict:
    """精华全文重建（v1.2）：从云端精华列表按分片标记重建完整文本。"""
    group = await _param("group", "")
    rid = await _param("id", "")
    if not group or not str(rid).isdigit():
        return error_response("group and id required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
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
    """精华删除（v1.2）：逐分片移出精华 → 资源软删。"""
    group = await _param("group", "")
    rid = await _param("id", "")
    if not group or not str(rid).isdigit():
        return error_response("group and id required", status_code=400)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    try:
        task_id = await s.ingest.submit_essence_delete(group, int(rid))
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id})


async def api_fetch(s: Services) -> dict:
    """Fetch an external URL into group files, a group album or essence.

    Albums accept images (direct upload) and videos (long-video sharding);
    an optional ``convert_to`` extension performs format conversion first.
    """
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
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
        task_id = await s.ingest.submit_fetch(
            group,
            url,
            name=str(payload.get("name") or "").strip(),
            to_album=bool(payload.get("to_album")),
            album_name=str(payload.get("album_name") or "").strip(),
            to_essence=bool(payload.get("to_essence")),
            convert_to=convert_to,
            lossy=bool(payload.get("lossy")),
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


async def api_file_replace_name(s: Services) -> dict:
    """改名（下载-重传）：下载原件→新名重传→删旧。"""
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
    """移动文件到指定文件夹（本地索引操作）。"""
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
        return error_response(str(e), 404)
    return json_response({"task_id": task_id})


async def api_file_uri(s: Services) -> dict:
    """按 cloud:// URI 定位资源（v1.7 可编码化引用；程序化查询面）。"""
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


async def _resolve_download_link(s: Services, group: str, fid: int):
    """解析单资源下载直链（file 类型；分卷不支持单链接）。返回 (url, name)。"""
    detail = await s.store.get_resource_detail(
        group, fid
    ) or await s.store.get_resource_any(fid)
    if not detail:
        raise ValueError(f"resource {fid} not found")
    if (detail.get("meta") or {}).get("volumes"):
        raise ValueError(
            "分卷/视频资源不支持单链接：请使用「下载」/「出库导出」或本机下载服务地址"
        )
    name = detail.get("name") or "download"
    fresh = await s.ops._fresh_file(
        str(detail.get("group_id") or group),
        name,
        int(detail.get("size") or 0),
        detail.get("folder_id") or None,
    )
    fid2, busid = fresh or (detail.get("source_ref"), detail.get("busid") or 0)
    url = await s.api.get_group_file_url(
        str(detail.get("group_id") or group), fid2, busid, name
    )
    return url, name


async def api_file_link(s: Services) -> dict:
    """复制下载直链（v1.5.1）：实时解析新鲜 file_id → QQ CDN 直链（可交付外部，时效性由 QQ 决定）。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    fid = qi(await _param("id", "0")) or 0
    if fid <= 0:
        return error_response("id required", status_code=400)
    detail = await s.store.get_resource_detail(
        group, fid
    ) or await s.store.get_resource_any(fid)
    if not detail:
        return error_response(f"resource {fid} not found", status_code=404)
    if (detail.get("meta") or {}).get("volumes"):
        return error_response(
            "分卷/视频资源不支持单链接：请使用「下载」/「出库导出」或本机下载服务地址",
            400,
        )
    name = detail.get("name") or "download"
    fresh = await s.ops._fresh_file(
        str(detail.get("group_id") or group),
        name,
        int(detail.get("size") or 0),
        detail.get("folder_id") or None,
    )
    fid2, busid = fresh or (detail.get("source_ref"), detail.get("busid") or 0)
    try:
        url = await s.api.get_group_file_url(
            str(detail.get("group_id") or group), fid2, busid, name
        )
    except Exception as e:
        return error_response(
            f"直链获取失败（上游可能暂时不可用，可改用「下载」/「出库导出」或本机下载服务）: {e}",
            502,
        )
    return json_response(
        {"url": url, "name": name, "note": "QQ 文件直链有时效性，请及时下载"}
    )


async def api_download_address(s: Services) -> dict:
    """本机下载服务地址（v1.6）：外部客户端可直接访问本机接口拉取云端文件。"""
    if not s.dlserver or not s.dlserver.enabled:
        return error_response(
            "下载服务未开启：请在插件配置开启 download_server_enabled", 400
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
    return json_response(info)


async def api_folder_create(s: Services) -> dict:
    """新建群文件目录（v1.6：对接 create_group_file_folder）。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    name = str(payload.get("name") or "").strip()
    if not (0 < len(name) <= 60):
        return error_response("name length 1..60", status_code=400)
    try:
        task_id = await s.ops.submit_create_folder(group, name)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id, "group": group, "name": name})


async def api_file_download(s: Services) -> dict | object:
    """下载：实时取群文件直链 → 流式代理转发（Page iframe 受限，必须经 bridge.download）。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    fid = qi(await _param("id", "0")) or 0
    if fid <= 0:
        return error_response("id required", status_code=400)
    import httpx
    from fastapi.responses import StreamingResponse

    try:
        target, name = await s.ops.download_info(group, fid)
    except ValueError as e:
        # 全局回退：id 在指定群不存在 → 跨库定位（统一管理视图行级群可能过期）
        try:
            d2 = (
                await s.store.get_resource_detail("*", fid)
                if False
                else await s.store.get_resource_any(fid)
            )
        except Exception:
            d2 = None
        if d2:
            target, name = await s.ops.download_info(d2["group_id"], fid)
        else:
            return error_response(f"{e}（全局亦无 id={fid}）", status_code=404)
    # RFC5987 文件名（响应头只能用 latin-1；中文/特殊字符必须百分号编码）
    from urllib.parse import quote as _quote

    safe_name = (name or "download").replace('"', "_")
    ascii_file = "download"
    disp = (
        f"attachment; filename=\"{ascii_file}\"; filename*=UTF-8''{_quote(safe_name)}"
    )
    from pathlib import Path as _Path

    # 分卷重组：本地临时文件 → 直接文件响应；单文件：URL 流式代理
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
    """群内文件扫描（与群信息扫描严格区分，docs/00 G-scan）：
    - mode=all：全部被管理群的群内文件列表刷新 + 容量联动
    - mode=range：指定 group_ids（默认=容量未知首群及其上方 2 群）
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
    """手动触发该群云端文件扫描（全量同步，入队限速执行）。"""
    group = await _param("group", "")
    if group:
        if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
            return error_response("group not managed", status_code=403)
        task_id = await s.queue.submit("sync", target=group)
        return json_response({"task_id": task_id, "groups": 1})
    # 无指定群 → 语义明确：请用 files/scan（全量/范围），避免隐式全量拉取
    return error_response(
        "请使用 files/scan 进行文件扫描（mode=all 全量 / mode=range 范围）——"
        "避免无明确目标的隐式全量云端拉取",
        status_code=400,
    )


async def api_file_detail(s: Services) -> dict:
    """文件详情（含分卷信息/哈希；Page 行操作「详情」）。"""
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


async def api_file_verify(s: Services) -> dict:
    """完整性校验（分卷：逐卷 sha256 比对 + 总哈希；单文件：无哈希则提示）。"""
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
    meta = d.get("meta") or {}
    if not meta.get("volumes"):
        return json_response(
            {
                "mode": "none",
                "ok": True,
                "message": "单文件无持久化哈希（分卷资源才有重组校验）",
            }
        )
    report = await s.ops.verify_volumes(
        group, d["resource_id"], meta.get("total_sha256")
    )
    return json_response(report)


async def api_queue_events(s: Services):
    """SSE：OpQueue 事件流（排队/开始/重试/完成/失败）。"""

    async def events():
        agen = s.queue.subscribe()
        try:
            while True:
                try:
                    # CT-6 心跳保持：静默期周期发心跳，供前端断线自愈判定（I5）
                    ev = await asyncio.wait_for(
                        agen.__anext__(), timeout=SSE_HEARTBEAT_SEC
                    )
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                except StopAsyncIteration:
                    return
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception:
            # SSE 稳定性：单次异常不炸会话（浏览器可重连）
            yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            await agen.aclose()

    return stream_response(events())


async def api_preview_policy(s: Services) -> dict:
    """CT-9 预览策略查询：?ext=.mp4 -> {ext, type, mode, template}。"""
    ext = (await _param("ext", "")).strip().lower()
    ext_overrides = s.config.get("type_ext_overrides") or {}
    ftype = classify_with_overrides(f"f{ext}", ext_overrides) if ext else "other"
    policy = preview_policy_for(ftype, s.config.get("preview_policy") or {})
    return json_response({"ext": ext, "type": ftype, **policy})


async def api_meta_classify(s: Services) -> dict:
    """CT-9 分类默认表（N6 数据驱动）：前端类型 chips 与本地过滤数据源。"""
    return json_response({"ext_types": FILE_TYPE_EXT, "labels": FILE_TYPE_LABEL})


# ---- 配置中心（D-7/T-7：归纳分类、便利优先；独立于任务体系） ----

# 分组顺序（配置 Tab 渲染顺序；配置项总表唯一口径）
_CONFIG_GROUPS = [
    "同步与凋零",
    "入库与组合存储",
    "桥接与网盘",
    "下载服务",
    "任务与队列",
    "页面与预览",
    "权限与发布",
]


async def api_config_get(s: Services) -> dict:
    """配置中心（分组渲染数据）：_conf_schema 分组 + 当前值（敏感项脱敏）。

    返回 {groups: [{name, items: [{key, value, default, description, type}]}],
          reload_required: [keys]} —— 前端按组渲染、搜索、危险项高亮。
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
        }
        # 敏感项脱敏（HL-11）
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
    """配置中心保存：整组写回（保留未提交键原值）+ 持久化到宿主配置。

    Body: {values: {key: value}}（仅提交变更键）；敏感项 "***" 表示不修改。
    返回 {saved: [keys], reload_required: [keys]}。
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
    masked_keys = {"openlist_password", "openlist_token", "download_token"}
    normalized: dict = {}
    for key, value in values.items():
        if key not in schema:
            continue
        if key in masked_keys and value == "***":
            continue
        item_value = value
        # 类型归一（schema type 对齐，防脏类型入库）
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
    # 持久化到宿主插件配置 JSON（与桥接保存同路径），并在内存 s.config 同步
    try:
        config_path = (
            Path(s.store._db_path).parent.parent
            / "config"
            / "astrbot_plugin_group_cloud_storage_config.json"
        )
        if not config_path.exists():
            config_path = Path(
                "/AstrBot/data/config/astrbot_plugin_group_cloud_storage_config.json"
            )
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8-sig") as f:
                cfg_file = json.load(f)
        else:
            cfg_file = {}
        cfg_file.update(normalized)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg_file, f, indent=4, ensure_ascii=False)
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


async def api_album_video_preview(s: Services) -> dict:
    """相册视频关键帧 GIF 预览（v2.5）：base64 内联展示。"""
    payload = await json_body()
    group = pick(payload, "group", required=True, empty_allowed=False)
    album_id = pick(payload, "album_id", required=True, empty_allowed=False)
    name = pick(payload, "name", default="")
    if not await s.scan.is_page_managed(group, s.config.get("managed_groups", [])):
        return error_response("group not managed", status_code=403)
    if not s.ingest:
        return error_response("ingest service not ready", status_code=500)
    try:
        out = await s.ingest.video_preview_gif(group, album_id, name)
    except TimeoutError as e:
        return error_response(str(e), status_code=504)
    except ValueError as e:
        return error_response(str(e), status_code=404)
    return json_response(out)


async def api_file_convert_volumes(s: Services) -> dict:
    """化整为零（v2.8）：云端已有大文件 → 分卷存储（下载→切分→逐卷上传→删原件）。"""
    group = await _param("group", "")
    if not group or not await s.scan.is_page_managed(
        group, s.config.get("managed_groups", [])
    ):
        return error_response("group not managed", status_code=403)
    payload = await json_body()
    fid = pick(payload, "id", cast=int, required=True)
    # 2026-09-03 C-4：可选分卷压缩（zip 可逆；下载重组自动解压）
    compress = bool(payload.get("compress"))
    try:
        task_id = await s.ops.submit_convert_volumes(group, fid, compress=compress)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return json_response({"task_id": task_id})


async def api_files_batch_delete(s: Services) -> dict:
    """批量删除文件（v2.3）：items=[{id,group}] 逐个入队，逐项失败不阻塞整体。"""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    managed = s.config.get("managed_groups", [])
    submitted, failed = 0, []
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
        try:
            task_id = await s.ops.submit_delete(gid, fid)
            submitted += 1
        except ValueError as e:
            failed.append(f"id={fid}: {e}")
    return json_response({"submitted": submitted, "failed": failed})


async def api_files_batch_move(s: Services) -> dict:
    """批量移动文件（v2.3）：items=[{id,group}] 移至同一目录。"""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    folder_id = pick(payload, "folder_id", required=True, empty_allowed=False)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    managed = s.config.get("managed_groups", [])
    submitted, failed = 0, []
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
        try:
            await s.ops.submit_move(gid, fid, str(folder_id))
            submitted += 1
        except ValueError as e:
            failed.append(f"id={fid}: {e}")
    return json_response({"submitted": submitted, "failed": failed})


async def api_files_batch_tags(s: Services) -> dict:
    """批量设置标签（v2.3）：items=[{id,group}] + tags（本地索引直写）。"""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    tags = pick(payload, "tags", cast=list, required=True)
    if not items or len(items) > 200:
        return error_response("items required (1..200)", status_code=400)
    if any(not isinstance(t, str) or len(t) > 24 for t in tags):
        return error_response("tag must be string(<=24)", status_code=400)
    clean = sorted({t.strip() for t in tags if t.strip()})
    managed = s.config.get("managed_groups", [])
    ids = []
    for it in items:
        try:
            fid = pick(it, "id", cast=int, required=True)
            gid = pick(it, "group", required=True, empty_allowed=False)
        except ApiValidationError:
            continue
        if not await s.scan.is_page_managed(gid, managed):
            continue
        ids.append(fid)
    for fid in ids:
        await s.store.update_resource_tags(fid, clean)
    return json_response({"updated": len(ids), "tags": clean})


async def api_files_links(s: Services) -> dict:
    """批量复制下载直链（v2.3）：items=[{id,group}]，上限 20（实时解析，不持久化）。"""
    payload = await json_body()
    items = pick(payload, "items", cast=list, required=True)
    if not items or len(items) > 20:
        return error_response("items required (1..20)", status_code=400)
    managed = s.config.get("managed_groups", [])
    links, errors = [], []
    for it in items:
        try:
            fid = pick(it, "id", cast=int, required=True)
            gid = pick(it, "group", required=True, empty_allowed=False)
        except ApiValidationError as e:
            errors.append(str(e))
            continue
        if not await s.scan.is_page_managed(gid, managed):
            errors.append(f"group {gid} not managed")
            continue
        try:
            url, name = await _resolve_download_link(s, gid, fid)
            links.append({"id": fid, "name": name, "url": url})
        except Exception as e:
            errors.append(f"id={fid}: {e}")
    return json_response({"links": links, "errors": errors})


# ---- Bridge API handlers (REQ-05/08/12/16/17) ----


# ---------- 任务台账与控制（v15，D-6：暂停/继续/中断/撤销 + 操作流） ----------

async def api_tasks(s: Services) -> dict:
    """任务台账查询（任务 Tab）：state/kind/target 过滤 + 分页。"""
    payload = await json_body()
    state = pick(payload, "state", default=None) if payload else None
    kind = pick(payload, "kind", default=None) if payload else None
    target = pick(payload, "target", default=None) if payload else None
    limit = int(pick(payload, "limit", default=100) or 100) if payload else 100
    offset = int(pick(payload, "offset", default=0) or 0) if payload else 0
    tasks = await s.task_control.list_tasks(
        state=state, kind=kind, target=target, limit=limit, offset=offset
    )
    return json_response({"tasks": tasks, "total": len(tasks), "limit": limit, "offset": offset})


async def api_tasks_queue(s: Services) -> dict:
    """OpQueue 实时状态（深度/运行中/暂停挂起/最近记录）。"""
    return json_response(await s.task_control.queue_status())


async def api_tasks_pause(s: Services) -> dict:
    """暂停任务（排队=挂起；运行中=协作式，下一检查点生效）。"""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.pause(task_id))


async def api_tasks_resume(s: Services) -> dict:
    """继续（恢复）任务。"""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.resume(task_id))


async def api_tasks_interrupt(s: Services) -> dict:
    """中断任务：排队=直接移除；运行中=协作式取消（检查点）；暂停挂起=置终态。"""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if not task_id:
        return error_response("task_id required", status_code=400)
    return json_response(await s.task_control.interrupt(task_id))


async def api_tasks_undo(s: Services) -> dict:
    """撤销任务：
    - {task_id}：未执行=丢弃；已完成=按可逆性矩阵补偿（移动反向/改名恢复）
    - {group_id, id}：文件标签直连操作快照恢复
    - 删除类操作云端不可逆 → 明示「不可撤销」
    """
    payload = await json_body()
    task_id = (payload or {}).get("task_id")
    group_id = (payload or {}).get("group_id")
    rid = (payload or {}).get("id")
    return json_response(
        await s.task_control.undo(
            task_id=task_id, group_id=group_id,
            resource_id=int(rid) if isinstance(rid, int) else None,
        )
    )


async def api_tasks_ops(s: Services) -> dict:
    """操作流记录（task_id 或 {group_id,id} 直接操作定位）。"""
    payload = await json_body()
    task_id = (payload or {}).get("task_id", "")
    if task_id:
        ops = await s.task_control.ops(task_id)
    else:
        group_id = (payload or {}).get("group_id")
        rid = (payload or {}).get("id")
        if not isinstance(rid, int) or not group_id:
            return error_response("task_id 或 (group_id, id) required", status_code=400)
        op = await s.store.ops_last_for_resource("tags", rid)
        ops = [op] if op is not None else []
    return json_response({"task_id": task_id, "ops": ops})


# ---------- D-4 凋零对账端点（17 号规格） ----------


async def api_sync_withering(s: Services) -> dict:
    """手动触发凋零差分对账（17 号规格端点）。

    Body: {group_ids?: [...]}（空 = 全部受管群）
    入队 diff_file_scan，返回 task_id。
    """
    await _ensure_ready(s)
    try:
        payload = await json_body()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ids = (payload or {}).get("group_ids")
    managed = s.config.get("managed_groups", [])
    if isinstance(ids, list) and ids:
        valid = []
        for gid in ids:
            g = str(gid)
            if g and await s.scan.is_page_managed(g, managed):
                valid.append(g)
        if not valid:
            return error_response("no valid managed groups", status_code=400)
        task_id = await s.queue.submit(
            "diff_file_scan", target="*", payload={"mode": "diff", "groups": valid}
        )
        return json_response({"task_id": task_id, "groups": len(valid)})
    task_id = await s.queue.submit("diff_file_scan", target="*", payload={"mode": "diff"})
    return json_response({"task_id": task_id, "mode": "all"})


async def api_sync_status(s: Services) -> dict:
    """凋零对账调度状态（17 号规格端点）。

    返回 {auto_scan_hours, last_diff_scan, running_diff_scans, queue_status}。
    """
    await _ensure_ready(s)
    hours = float(s.config.get("auto_scan_interval_hours", 6) or 0)
    # 查询最近一次 diff_file_scan 任务
    recent = await s.task_control.list_tasks(kind="diff_file_scan", limit=1)
    last_scan = None
    if recent:
        r = recent[0]
        last_scan = {
            "task_id": r.get("task_id"),
            "state": r.get("state"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "error": r.get("error"),
        }
    # 查询正在运行的 diff_file_scan 数量
    running = await s.task_control.list_tasks(kind="diff_file_scan", state="running", limit=100)
    queue_status = await s.task_control.queue_status()
    return json_response({
        "auto_scan_hours": hours,
        "auto_scan_enabled": hours > 0,
        "last_diff_scan": last_scan,
        "running_count": len(running),
        "queue": queue_status,
    })


async def api_tasks_resume_pending(s: Services) -> dict:
    """断点续传重提（ADR-0005 白名单 pending 恢复执行）。

    查询 op_ledger 中 state=pending 且 kind 在白名单内的任务，逐条入队恢复。
    白名单：convert_volumes, video_upload, netdisk_index（与 ledger_reconcile 一致）。
    """
    await _ensure_ready(s)
    pending = await s.task_control.list_tasks(state="pending", limit=200)
    if not pending:
        return json_response({"resumed": 0, "note": "无待恢复任务"})
    _BREAKPOINT_KINDS = {"convert_volumes", "video_upload", "netdisk_index"}
    resumed = 0
    skipped = 0
    for row in pending:
        kind = row.get("kind", "")
        if kind not in _BREAKPOINT_KINDS:
            skipped += 1
            continue
        task_id = row.get("task_id", "")
        target = row.get("target", "")
        payload = {}
        try:
            import json as _json
            raw = row.get("payload")
            if isinstance(raw, str):
                payload = _json.loads(raw)
            elif isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
        try:
            await s.queue.submit(kind, target=target, payload=payload)
            resumed += 1
            # 标记为 running（避免重复重提）
            await s.task_control.on_state(task_id, kind, target, payload, "running")
        except Exception as e:
            logger.warning(f"[tasks-resume] re-submit {task_id} ({kind}) failed: {e}")
    return json_response({
        "resumed": resumed,
        "skipped": skipped,
        "total_pending": len(pending),
    })


def register_page_apis(context: Context, s: Services) -> None:
    # 2026-09-03 拆分为 webapi_base/webapi_debug/webapi_ext/webapi_netdisk：
    # API 目录采集统一包装（capture_all）在全部注册之前执行一次。
    capture_all(context)
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups", _Bound(s, api_groups), ["GET"], "群清单"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/accounts",
        _Bound(s, api_accounts),
        ["GET"],
        "账号清单（统一/单独管理）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/scan", _Bound(s, api_scan), ["POST"], "触发群扫描"
    )
    # 注意：bridge SDK 仅支持 GET/POST（无 PATCH/PUT），故 batch/order 用 POST 提交
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/batch",
        _Bound(s, api_groups_batch_update),
        ["POST"],
        "批量改名/标号",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/batch-ops",
        _Bound(s, api_groups_batch_actions),
        ["POST"],
        "批量群操作（改名/加群方式/备注）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/order", _Bound(s, api_groups_order), ["POST"], "群排序"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/remove",
        _Bound(s, api_groups_remove),
        ["POST"],
        "移除管理条目",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/removed",
        _Bound(s, api_groups_removed),
        ["GET"],
        "已移除管理的群",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/groups/restore",
        _Bound(s, api_groups_restore),
        ["POST"],
        "恢复管理",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files", _Bound(s, api_files), ["GET"], "文件列表/检索"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/stat", _Bound(s, api_stat), ["GET"], "群统计"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/events", _Bound(s, api_queue_events), ["GET"], "操作队列 SSE"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/upload/prepare",
        _Bound(s, api_file_upload_prepare),
        ["POST"],
        "上传准备",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/recommend-group",
        _Bound(s, api_files_recommend_group),
        ["GET"],
        "推荐上传群（缺省规则：群号最小且剩余空间足够）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/upload/<token>",
        _Bound(s, api_file_upload),
        ["POST"],
        "上传文件",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/delete", _Bound(s, api_file_delete), ["POST"], "删除文件"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/replace_name",
        _Bound(s, api_file_replace_name),
        ["POST"],
        "下载并改名重传",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/albums/media",
        _Bound(s, api_album_media),
        ["GET"],
        "相册媒体实时列表",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/albums/video-preview",
        _Bound(s, api_album_video_preview),
        ["POST"],
        "相册视频关键帧 GIF 预览",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/essence/save",
        _Bound(s, api_essence_save),
        ["POST"],
        "精华文本入库（拆分存储）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/essence/text",
        _Bound(s, api_essence_text),
        ["GET"],
        "精华全文重建",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/essence/delete",
        _Bound(s, api_essence_delete),
        ["POST"],
        "精华删除（分片清理）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/fetch", _Bound(s, api_fetch), ["POST"], "HTTP/FTP 外部文件入库"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/tags", _Bound(s, api_file_tags), ["POST"], "资源标签设置"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/tagcloud", _Bound(s, api_tagcloud), ["GET"], "标签云聚合"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/move", _Bound(s, api_file_move), ["POST"], "移动文件"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/download",
        _Bound(s, api_file_download),
        ["GET"],
        "文件直链",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/link",
        _Bound(s, api_file_link),
        ["GET"],
        "复制下载直链（外部交付）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/download/address",
        _Bound(s, api_download_address),
        ["GET"],
        "本机下载服务地址",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/folder-create",
        _Bound(s, api_folder_create),
        ["POST"],
        "新建群文件目录",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/uri",
        _Bound(s, api_file_uri),
        ["GET"],
        "cloud:// URI 定位资源",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/scan",
        _Bound(s, api_files_scan),
        ["POST"],
        "群内文件扫描",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/sync",
        _Bound(s, api_files_sync),
        ["POST"],
        "单群列表刷新",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/convert-volumes",
        _Bound(s, api_file_convert_volumes),
        ["POST"],
        "云端大文件转分卷",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/batch-delete",
        _Bound(s, api_files_batch_delete),
        ["POST"],
        "批量删除文件",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/batch-move",
        _Bound(s, api_files_batch_move),
        ["POST"],
        "批量移动文件",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/batch-tags",
        _Bound(s, api_files_batch_tags),
        ["POST"],
        "批量设置标签",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/links",
        _Bound(s, api_files_links),
        ["POST"],
        "批量下载直链",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/detail", _Bound(s, api_file_detail), ["GET"], "文件详情"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/files/verify",
        _Bound(s, api_file_verify),
        ["GET"],
        "完整性校验（分卷逐卷 sha256 比对）",
    )

    # ---- Bridge API endpoints (REQ-05/08/12/16/17) ----

    context.register_web_api(
        f"/{PLUGIN_NAME}/preview/policy",
        _Bound(s, api_preview_policy),
        ["GET"],
        "预览策略查询（CT-9）",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/meta/classify",
        _Bound(s, api_meta_classify),
        ["GET"],
        "类型分类默认表（CT-9 数据驱动）",
    )

    # --- Netdisk file operations ---
    # v15 任务台账与控制（D-6）：任务 Tab 后端
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks", _Bound(s, api_tasks), ["GET", "POST"], "任务台账查询"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/queue",
        _Bound(s, api_tasks_queue),
        ["GET"],
        "OpQueue 实时状态",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/pause", _Bound(s, api_tasks_pause), ["POST"], "暂停任务"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/resume", _Bound(s, api_tasks_resume), ["POST"], "继续任务"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/interrupt",
        _Bound(s, api_tasks_interrupt),
        ["POST"],
        "中断任务",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/undo", _Bound(s, api_tasks_undo), ["POST"], "撤销任务"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/ops", _Bound(s, api_tasks_ops), ["POST"], "操作流记录"
    )
    # D-7/T-7 配置中心
    context.register_web_api(
        f"/{PLUGIN_NAME}/config/get", _Bound(s, api_config_get), ["GET"], "配置中心（分组渲染）"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/config/save", _Bound(s, api_config_save), ["POST"], "配置中心保存"
    )

    # D-4 凋零对账端点（17 号规格：差分对账调度状态/手动触发）
    context.register_web_api(
        f"/{PLUGIN_NAME}/sync/withering", _Bound(s, api_sync_withering), ["POST"], "手动触发凋零差分对账"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/sync/status", _Bound(s, api_sync_status), ["GET"], "凋零对账调度状态"
    )

    # 断点续传重提（ADR-0005 白名单 pending 恢复执行）
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/resume-pending", _Bound(s, api_tasks_resume_pending), ["POST"], "断点续传重提（白名单 pending 恢复）"
    )

    # ---- 域模块注册（2026-09-03 拆分：ext/netdisk） ----
    register_ext_apis(context, s)
    register_netdisk_apis(context, s)
