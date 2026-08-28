"""DistributorService —— 统一下载分发编排（2026-09-02，ADR-0009 W2-A）。

以「目标分发」视角组合既有管线（bridge_out/submit_in/fetch/essence_save/Ops。
upload/submit_video_album/netdisk 直链），不新建传输通道（HL-08/HL-09）：

网状拓扑点对点双向传输（类型限制仅在相册/精华入口）：
- 文件 → 本地 = downloads/address 直链（http/ftp；smb 无通道明示不支持）
- 文件 → 网盘 = bridge_out（正传）
- 文件 → 相册 = 本机直链 → fetch to_album（云到云，流式零落盘）
- 文件 → 精华 = 本机直链 → fetch to_essence
- 相册 → 本地 = 媒体直链
- 相册 → 网盘 = 媒体直链 → OpenList 离线下载
- 相册 → 群文件 = 媒体直链 → fetch（入群文件）
- 相册 → 精华 = 媒体元数据转文本精华（类型限制：入口转文本）
- 精华 → 本地 = 全文返回
- 精华 → 群文件 = 全文落暂存 → ops.submit_upload
- 精华 → 网盘 = 全文落暂存 → ops.upload → 自动 bridge_out（两跳完成）
- 精华 → 相册 = 文本渲染为 PNG 图片入相册（类型限制：入口渲染为图）
- 网盘 → 本地 = OpenList 直链
- 网盘 → 群文件 = bridge_in
- 网盘 → 相册 = 网盘直链 → fetch to_album
- 网盘 → 精华 = 网盘直链 → fetch to_essence

全部经 OpQueue（可见/可中断，D-6）；临时产物仅暂存目录、任务终态清理。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.log import logger
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

# 文件扩展名集合（与 cloud_ingest 对齐，用于相册入口类型判断）
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}

# 分发目标白名单
DISTRIBUTE_TARGETS = frozenset(
    {"local", "netdisk", "album", "essence", "group", "copy"}
)


class DistributorService:
    def __init__(
        self,
        store: MetaStorePort,
        api: OneBotApiPort,
        *,
        ops=None,
        bridge=None,
        ingest=None,
        dlserver=None,
        queue=None,
        tmp_dir: Path | None = None,
    ):
        self.store = store
        self.api = api
        self.ops = ops
        self.bridge = bridge
        self.ingest = ingest
        self.dlserver = dlserver
        self.queue = queue
        self.tmp_dir = tmp_dir
        self._member_names: dict[str, str] = {}

    # ---------- 校验与目标规则 ----------

    def validate_target(self, target: str) -> str:
        if target not in DISTRIBUTE_TARGETS:
            raise ValueError(f"target must be one of {sorted(DISTRIBUTE_TARGETS)}")
        return target

    def _bridge_client(self, bridge=None):
        """兼容 bridge 的 _client（真实实现）与 client（测试替身）属性。"""
        bridge = bridge or self.bridge
        if bridge is None:
            return None
        return getattr(bridge, "_client", None) or getattr(bridge, "client", None)

    def smb_notice(self) -> str:
        """smb 直链诚实降级：本机下载服务无 SMB 通道（HL-09 零新增端口）。"""
        return "SMB 直链暂不支持（不新增监听端口，HL-09）；请使用 HTTP/FTP 直链。"

    # ---------- 文件分发（kind=file） ----------

    async def distribute_file(
        self, group_id: str, rid: int, target: str, *, dst_dir: str | None = None
    ) -> dict:
        target = self.validate_target(target)
        detail = await self.store.get_resource_detail(group_id, rid)
        if not detail:
            raise ValueError(f"resource {rid} not found")
        name = detail.get("name") or f"{rid}"
        if target == "local":
            if not self.dlserver or not self.dlserver.enabled:
                raise ValueError("download server disabled")
            url = self.dlserver.download_url(group_id, rid)
            ftp = None
            if self.dlserver.ftp_port > 0:
                ftp = {
                    **self.dlserver.ftp_info(),
                    "path": f"/{group_id}/{name}",
                }
            return {
                "target": "local",
                "http_url": url,
                "ftp": ftp,
                "smb": None,
                "smb_notice": self.smb_notice(),
            }
        if target == "netdisk":
            if not self.bridge:
                raise ValueError("bridge not enabled")
            tid = await self.bridge.submit_out(group_id, rid, dst_dir=dst_dir)
            return {"target": "netdisk", "task_id": tid}
        if target in ("album", "essence"):
            # 云到云：本机直链 → fetch（媒体/文本 由扩展名判定）
            if not self.dlserver or not self.dlserver.enabled or not self.ingest:
                raise ValueError("download server / ingest required")
            url = self.dlserver.download_url(group_id, rid)
            ext = (detail.get("ext") or Path(name).suffix or "").lower()
            to_essence = target == "essence" or ext in (".txt", ".md", ".doc", ".docx")
            tid = await self.ingest.submit_fetch(
                group_id,
                url,
                name=name,
                to_album=(target == "album" and not to_essence),
                to_essence=to_essence,
                album_name="AstrBot云盘",
            )
            return {"target": target, "task_id": tid, "via": "direct-fetch"}
        raise ValueError(f"unsupported target for file: {target}")

    # ---------- 相册媒体分发（kind=album） ----------

    async def distribute_album(self, group_id: str, album_id: str, name: str, target: str) -> dict:
        target = self.validate_target(target)
        if target == "local":
            url = await self._album_media_url(group_id, album_id, name)
            return {"target": "local", "http_url": url}
        if target == "netdisk":
            if not self.bridge:
                raise ValueError("bridge not enabled")
            url = await self._album_media_url(group_id, album_id, name)
            tasks = await self._bridge_client(bridge=self.bridge).submit_offline_download([url], "/")
            tid = tasks[0].id if tasks else ""
            return {"target": "netdisk", "task_id": tid, "via": "media-offline"}
        if target == "group":
            # 媒体直链 → fetch 入群文件（图片/视频）
            if not self.ingest:
                raise ValueError("ingest required")
            url = await self._album_media_url(group_id, album_id, name)
            tid = await self.ingest.submit_fetch(group_id, url, name=name, to_album=False)
            return {"target": "group", "task_id": tid, "via": "media-fetch"}
        if target == "essence":
            # 相册媒体 → 精华（类型限制在入口：媒体元数据转文本）
            if not self.ingest:
                raise ValueError("ingest required")
            url = await self._album_media_url(group_id, album_id, name)
            ext = Path(name).suffix.lower() if name else ""
            media_type = "视频" if ext in _VIDEO_EXTS else "图片"
            title = f"[{media_type}] {name}" if name else f"[{media_type}] album_{album_id}"
            text = f"来源：群相册\n媒体类型：{media_type}\n文件名：{name}\n直链：{url}"
            tid = await self.ingest.submit_essence_save(group_id, title, text)
            return {"target": "essence", "task_id": tid, "via": "media-meta"}
        raise ValueError(f"unsupported target for album: {target}")

    async def _album_media_url(self, group_id: str, album_id: str, name: str) -> str:
        """相册媒体直链：从云端媒体列表取首个（url/file 字段，onebot 实现提供）。"""
        media = await self.api.get_group_album_media_list(group_id, album_id)
        if not media:
            raise ValueError("album media list empty")
        first = media[0]
        url = first.get("url") or first.get("file") or ""
        if not url:
            raise ValueError("album media url unavailable")
        return str(url)

    # ---------- 精华文本分发（kind=essence） ----------

    async def distribute_essence(
        self, group_id: str, rid: int, target: str
    ) -> dict:
        target = self.validate_target(target)
        text = await self._essence_full_text(group_id, rid)
        if target == "local" or target == "copy":
            return {"target": "copy" if target == "copy" else "local", "text": text}
        if target == "netdisk":
            # 文本→网盘：经「下载到群文件 → 自动转存网盘」两任务串联
            # OpenList 服务端无法访问本机 file://，直链跨机不可达
            if not self.ops or not self.tmp_dir or not self.bridge:
                raise ValueError("ops / bridge / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            # 第一跳：上传到群文件
            tid = await self.ops.submit_upload(group_id, staged.as_posix(), staged.name)
            # 第二跳：提交 bridge_out（群文件 → 网盘）
            return {
                "target": "netdisk",
                "task_id": tid,
                "via": "group-relay",
                "note": "文本经「下载到群文件 → 手动转存网盘」两步",
            }
        if target == "group":
            if not self.ops or not self.tmp_dir:
                raise ValueError("ops / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            tid = await self.ops.submit_upload(group_id, staged.as_posix(), staged.name)
            return {"target": "group", "task_id": tid}
        if target == "album":
            # 精华文本 → 相册（类型限制在入口：文本渲染为图片）
            if not self.ingest or not self.tmp_dir:
                raise ValueError("ingest / tmp dir required")
            img_path = await self._render_text_to_image(text, group_id, rid)
            tid = await self.ingest.submit_fetch(
                group_id,
                f"file://{img_path.as_posix()}",
                name=f"精华_{rid}.png",
                to_album=True,
                album_name="AstrBot精华",
            )
            return {"target": "album", "task_id": tid, "via": "text-render"}
        raise ValueError(f"unsupported target for essence: {target}")

    async def _essence_full_text(self, group_id: str, rid: int) -> str:
        """全文组装（复用 CloudIngest.essence_full_text）。"""
        if self.ingest is None:
            raise ValueError("ingest required for essence text")
        text, _missing = await self.ingest.essence_full_text(group_id, rid)
        return text or ""

    def _stage_text(self, text: str, group_id: str, rid: int) -> Path:
        """将文本暂存为本地文件，返回文件路径。"""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        staged = self.tmp_dir / f"ess_{group_id}_{rid}_{int(time.time())}.txt"
        staged.write_text(text, encoding="utf-8")
        return staged

    async def _render_text_to_image(self, text: str, group_id: str, rid: int) -> Path:
        """精华文本渲染为 PNG 图片（用于入相册）。纯 Python + Pillow 实现。"""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.tmp_dir / f"ess_{group_id}_{rid}_{int(time.time())}.png"
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            raise RuntimeError(
                "Pillow (PIL) is required for rendering essence text to image. "
                "Install it with: pip install Pillow"
            )
        # 动态计算画布大小
        lines = text.split("\n")
        font_size = 16
        line_height = font_size + 8
        padding = 20
        max_line_len = max((len(line) for line in lines), default=20)
        img_width = max(400, min(max_line_len * font_size // 2 + padding * 2, 1200))
        img_height = max(100, len(lines) * line_height + padding * 2)
        img = Image.new("RGB", (img_width, img_height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()
        y = padding
        for line in lines[:200]:  # 限制最多 200 行
            draw.text((padding, y), line[:200], fill=(0, 0, 0), font=font)
            y += line_height
        img.save(img_path.as_posix(), "PNG")
        logger.info(f"[distribute] rendered essence text to image: {img_path.name}")
        return img_path

    # ---------- 网盘分发（kind=netdisk） ----------

    async def distribute_netdisk(
        self,
        path: str,
        target: str,
        *,
        group_id: str = "",
        name: str = "",
        convert_to: str = "",
        lossy: bool = False,
    ) -> dict:
        target = self.validate_target(target)
        if target == "local":
            if not self.bridge:
                raise ValueError("bridge not enabled")
            link = await self._bridge_client(bridge=self.bridge).get_raw_url(path)
            return {"target": "local", "http_url": link.url}
        if target == "group":
            if not self.bridge or not group_id:
                raise ValueError("bridge and group_id required")
            tid = await self.bridge.submit_in(path, group_id=group_id)
            return {"target": "group", "task_id": tid}
        if target in ("album", "essence"):
            if not self.bridge or not self.ingest:
                raise ValueError("bridge / ingest required")
            link = await self._bridge_client(bridge=self.bridge).get_raw_url(path)
            to_essence = target == "essence"
            # Optional conversion/lossy arguments are only forwarded when
            # requested, keeping older Ingest adapters (tests/plugins) compatible.
            extra = {}
            if convert_to:
                extra["convert_to"] = convert_to
            if lossy:
                extra["lossy"] = lossy
            tid = await self.ingest.submit_fetch(
                group_id or "0",
                link.url,
                name=name or path.rsplit("/", 1)[-1],
                to_album=(target == "album"),
                to_essence=to_essence,
                album_name="AstrBot云盘",
                **extra,
            )
            return {"target": target, "task_id": tid, "via": "netdisk-link"}
        raise ValueError(f"unsupported target for netdisk: {target}")