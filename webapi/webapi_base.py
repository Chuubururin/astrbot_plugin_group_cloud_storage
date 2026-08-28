"""Page 后端 API 共享基础设施（2026-09-03 复杂度拆分，行为零变化）。

从 webapi.py 拆分：常量、参数读取、序列化、群缓存、convert_to 归一、
注册采集（catalog/handler 映射）与 _Bound 惰性绑定。webapi.py 与
webapi_debug / webapi_ext / webapi_netdisk 单向依赖本模块（无循环 import）。
"""

from __future__ import annotations

import re
import time as _time_mod

from astrbot.api.star import Context
from astrbot.api.web import error_response, request
from core.api_validate import ApiValidationError
from commands.handlers import Services

PLUGIN_NAME = "astrbot_plugin_group_cloud_storage"

# SSE 心跳间隔（秒，CT-6；前端 constants.SSE_HEARTBEAT_TIMEOUT_MS 以 3 倍判定）
SSE_HEARTBEAT_SEC = 30.0

# 相册媒体云端拉取超时（秒）
CLOUD_MEDIA_TIMEOUT = 12.0

# 2026-09-01 N-06：相册 image 模式的扩展名白名单（与 cloud_ingest._IMAGE_EXTS 对齐）
_ALBUM_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _is_image_name(name: str) -> bool:
    """名称是否为相册 image 模式允许的图片扩展名。"""
    from pathlib import Path

    return Path(name or "").suffix.lower() in _ALBUM_IMAGE_EXTS


async def _param(key: str, default: str = "") -> str:
    """统一取参：query 优先，其次 JSON body（bridge apiPost 不能带 query）。"""
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
    """惰性初始化：Page 首调时确保 store/queue 就绪（main 注入 ready 回调）。"""
    if s.ready is not None:
        await s.ready()


_TAG_RE = re.compile(r"^[\w\u4e00-\u9fa5\-_]{0,32}$")


def _group_item(g) -> dict:
    """群行序列化（groups / groups/removed 共用）。"""
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


# 受管群清单缓存（v2.10）：检索链路每击键不再全量拉群（万群规模关键）
_GROUP_LIST_CACHE: dict = {"key": None, "at": 0.0, "groups": None}


async def _managed_groups_cached(s: Services):
    """受管群清单缓存（v2.10）：仅返回 managed=1 的群。"""
    key = tuple(s.config.get("managed_groups", []) or [])
    now = _time_mod.monotonic()
    if _GROUP_LIST_CACHE["key"] == key and now - _GROUP_LIST_CACHE["at"] < 30.0:
        return _GROUP_LIST_CACHE["groups"]
    groups = await s.scan.list_page_groups(s.config.get("managed_groups", []))
    _GROUP_LIST_CACHE.update(key=key, at=now, groups=groups)
    return groups


# convert_to 白名单（fetch / netdisk/distribute / upload prepare 共用，带点扩展名）
_CONVERT_TO_EXT = {".mp4", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}


def _normalize_convert_to(value) -> str:
    """convert_to 白名单归一：合法返回带点扩展名（'.mp4'），空/非法返回 ''。"""
    v = str(value or "").strip().lstrip(".").lower()
    ext = f".{v}" if v else ""
    return ext if ext in _CONVERT_TO_EXT else ""


# ---------- 注册采集（debug/apis 目录 + invoke 处理器映射，ADR-0014） ----------

_PAGE_ROUTE_CATALOG: list[dict] = []
_PAGE_HANDLERS: dict[str, "_Bound"] = {}


def capture_all(ctx: Context) -> Context:
    """包装 context.register_web_api：采集目录条目 + 登记短路径→处理器。

    调用方（register_page_apis）在注册全部端点前调用一次。
    """
    orig = ctx.register_web_api

    def _capture(path: str, bound, methods: list, desc: str = "") -> None:
        full = (
            f"/api/v1/plugins/extensions/{PLUGIN_NAME}/"
            + path.replace(f"/{PLUGIN_NAME}/", "")
        )
        _PAGE_ROUTE_CATALOG.append(
            {"path": full, "method": ",".join(methods), "desc": desc}
        )
        _PAGE_HANDLERS[full.replace(f"/api/v1/plugins/extensions/{PLUGIN_NAME}/", "")] = bound
        return orig(path, bound, methods, desc)

    ctx.register_web_api = _capture
    return ctx


class _Bound:
    """注册适配器：AstrBot 以零参数调用 handler（asgi_runtime._call_view），
    业务参数（Services）通过闭包绑定。"""

    def __init__(self, s: Services, fn):
        self._s = s
        self._fn = fn

    async def __call__(self, **kwargs):
        """统一惰性初始化 + 透传动态路由参数（如 <token>）给 handler。

        参数校验失败（ApiValidationError）统一转 400；
        其他异常（DB/网络/逻辑）统一转 500 JSON（防止前端收到非 JSON 响应导致静默空数据）。
        """
        if self._s.ready is not None:
            await self._s.ready()
        try:
            return await self._fn(self._s, **kwargs)
        except ApiValidationError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(str(e), status_code=500)