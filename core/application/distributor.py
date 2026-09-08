"""DistributorService - unified download/distribution orchestration.

Composes the existing pipelines from a "target distribution" viewpoint
(bridge_out / submit_in / fetch / essence_save / ops.upload /
submit_video_album / netdisk direct links); no new transport channels are
created.

Mesh topology, point-to-point transfers in both directions (type
restrictions apply only at the album/essence entrances):
- file -> local = downloads direct link (http/sftp; smb has no channel and
  is explicitly unsupported)
- file -> netdisk = bridge_out (forward transfer)
- file -> album = local direct link -> fetch to_album (cloud to cloud,
  streaming, no disk staging)
- file -> essence = local direct link -> fetch to_essence
- album -> local = media direct link
- album -> netdisk = media direct link -> OpenList offline download
- album -> group files = media direct link -> fetch (into group files)
- album -> essence = media metadata converted to a text essence (type
  restriction: text conversion at the entrance)
- essence -> local = full text returned
- essence -> group files = full text staged -> ops.submit_upload
- essence -> netdisk = full text staged -> ops.upload -> bridge_out
  (two hops)
- essence -> album = text rendered to a PNG image into the album (type
  restriction: image rendering at the entrance)
- netdisk -> local = OpenList direct link
- netdisk -> group files = bridge_in
- netdisk -> album = netdisk direct link -> fetch to_album
- netdisk -> essence = netdisk direct link -> fetch to_essence

Everything goes through OpQueue (visible/interruptible); temporary
artifacts live only in the staging directory and are cleaned when the task
reaches a terminal state.
"""

from __future__ import annotations

import time
from pathlib import Path

from core.application.common import path_basename
from core.log import logger
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

# Candidate TrueType paths for text->image rendering, tried in order
# (cross-platform; the essence text is usually CJK, so CJK-capable fonts come
# first). Falls back to Pillow's built-in bitmap font when none is present.
_FONT_CANDIDATES = (
    # Linux (common distros)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def _load_render_font(ImageFont, size: int):
    """First available candidate TrueType font, else Pillow's default."""
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# File extension sets (aligned with cloud_ingest; used for album entrance
# type checks)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}

# Distribution target whitelist
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

    # ---------- Validation and target rules ----------

    def validate_target(self, target: str) -> str:
        if target not in DISTRIBUTE_TARGETS:
            raise ValueError(f"target must be one of {sorted(DISTRIBUTE_TARGETS)}")
        return target

    def _bridge_client(self, bridge=None):
        """Tolerate both bridge attribute names: _client (real implementation)
        and client (test double)."""
        bridge = bridge or self.bridge
        if bridge is None:
            return None
        return getattr(bridge, "_client", None) or getattr(bridge, "client", None)

    def smb_notice(self) -> str:
        """smb direct-link degradation notice: the local download server has
        no SMB channel (missing impacket or port disabled)."""
        return "SMB 直链未开启（需配置 download_smb_port 且安装 impacket）；请使用 HTTP/SFTP 直链。"

    # ---------- File distribution (kind=file) ----------

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
            out = self._address_info(group_id, rid, name)
            return {"target": "local", **out}
        if target == "netdisk":
            if not self.bridge:
                raise ValueError("bridge not enabled")
            tid = await self.bridge.submit_out(group_id, rid, dst_dir=dst_dir)
            return {"target": "netdisk", "task_id": tid}
        if target in ("album", "essence"):
            # Cloud to cloud: local direct link -> fetch (media/text decided
            # by extension)
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

    def _address_info(self, group_id: str, rid: int, name: str) -> dict:
        """Direct-link address set (http/sftp/smb) for one resource on the
        local download service; smb present only when the channel is up."""
        if not self.dlserver:
            raise ValueError("download server disabled")
        out: dict = {"http_url": self.dlserver.download_url(group_id, rid)}
        if self.dlserver.sftp_port > 0:
            out["sftp"] = {
                **self.dlserver.sftp_info(),
                "path": f"/{group_id}/{name}",
            }
        if (
            getattr(self.dlserver, "smb_port", 0) > 0
            and getattr(self.dlserver, "smb_available", False)
        ):
            out["smb"] = self.dlserver.smb_info(group_id, name)
            # Materialize the file into the SMB cache in the background so
            # the UNC path resolves shortly after the reply
            import asyncio as _aio

            try:
                _aio.get_running_loop().create_task(
                    self.dlserver.ensure_local(group_id, rid, name)
                )
            except RuntimeError:
                pass
        else:
            out["smb"] = None
            out["smb_notice"] = self.smb_notice()
        return out

    # ---------- Album media distribution (kind=album) ----------

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
            # Media direct link -> fetch into group files (images/videos)
            if not self.ingest:
                raise ValueError("ingest required")
            url = await self._album_media_url(group_id, album_id, name)
            tid = await self.ingest.submit_fetch(group_id, url, name=name, to_album=False)
            return {"target": "group", "task_id": tid, "via": "media-fetch"}
        if target == "essence":
            # Album media -> essence (type restriction at the entrance: media
            # metadata becomes text)
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
        """Album media direct link: match by filename first; fall back to the
        first entry."""
        media = await self.api.get_group_album_media_list(group_id, album_id)
        if not media:
            raise ValueError("album media list empty")
        picked = media[0]
        want = str(name or "").strip().lower()
        if want:
            for m in media:
                if not isinstance(m, dict):
                    continue
                video_name = (m.get("video") or {}).get("name") or ""
                cand = str(
                    m.get("name") or m.get("desc") or m.get("filename") or video_name
                ).strip().lower()
                if cand and (cand == want or want in cand or cand in want):
                    picked = m
                    break
        url = self._extract_media_url(picked)
        if not url:
            # If the preferred entry has no direct link, fall back to any
            # media that has one
            for m in media:
                url = self._extract_media_url(m)
                if url:
                    break
        if not url:
            raise ValueError("album media url unavailable")
        return str(url)

    @staticmethod
    def _extract_media_url(m: dict) -> str:
        """Support both shapes: flat {url} and the nested QQ
        image.photo_url[].url.url."""
        if not isinstance(m, dict):
            return ""
        flat = m.get("url") or m.get("file") or ""
        if isinstance(flat, str) and flat:
            return flat
        photos = (m.get("image") or {}).get("photo_url") or []
        for p in photos:
            u = (p or {}).get("url")
            if isinstance(u, dict) and u.get("url"):
                return str(u["url"])
            if isinstance(u, str) and u:
                return u
        return ""

    # ---------- Essence text distribution (kind=essence) ----------

    async def distribute_essence(
        self, group_id: str, rid: int, target: str
    ) -> dict:
        target = self.validate_target(target)
        text = await self._essence_full_text(group_id, rid)
        if target == "local":
            # Full text staged as a file and served over the local download
            # service (http/sftp/smb direct links); the text is also returned
            # so the browser side can copy it without a second fetch.
            if not self.dlserver or not self.dlserver.enabled or not self.tmp_dir:
                raise ValueError("download server / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            name = await self._essence_name(group_id, rid)
            addr = self.dlserver.register_staged(staged, f"{name}.txt")
            return {
                "target": "local",
                "text": text,
                "http_url": addr.get("http_url"),
                "sftp": addr.get("sftp"),
                "smb": addr.get("smb"),
                **({} if addr.get("smb") else {"smb_notice": self.smb_notice()}),
            }
        if target == "copy":
            return {"target": "copy", "text": text}
        if target == "netdisk":
            # Text -> netdisk: relayed through group files, because the
            # OpenList server cannot reach this host's file:// links
            if not self.ops or not self.tmp_dir or not self.bridge:
                raise ValueError("ops / bridge / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            # First hop: upload into group files
            tid = await self.ops.submit_upload(group_id, staged.as_posix(), staged.name)
            # Second hop: bridge_out (group files -> netdisk)
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
            # Essence text -> album (type restriction at the entrance: text
            # rendered to an image)
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
        """Assemble the full text (reuses CloudIngest.essence_full_text)."""
        if self.ingest is None:
            raise ValueError("ingest required for essence text")
        text, _missing = await self.ingest.essence_full_text(group_id, rid)
        return text or ""

    async def _essence_name(self, group_id: str, rid: int) -> str:
        """Resource display name (row name when available, else the id)."""
        row = await self.store.get_resource_any(rid)
        name = (row or {}).get("name") or f"essence_{rid}"
        return Path(name).name

    def _stage_text(self, text: str, group_id: str, rid: int) -> Path:
        """Stage the text as a local file and return its path."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        staged = self.tmp_dir / f"ess_{group_id}_{rid}_{int(time.time())}.txt"
        staged.write_text(text, encoding="utf-8")
        return staged

    async def _render_text_to_image(self, text: str, group_id: str, rid: int) -> Path:
        """Render essence text to a PNG image (for album import). Pure
        Python + Pillow implementation."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.tmp_dir / f"ess_{group_id}_{rid}_{int(time.time())}.png"
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as e:
            raise RuntimeError(
                "Pillow (PIL) is required for rendering essence text to image. "
                "Install it with: pip install Pillow"
            ) from e
        # Compute the canvas size dynamically
        lines = text.split("\n")
        font_size = 16
        line_height = font_size + 8
        padding = 20
        max_line_len = max((len(line) for line in lines), default=20)
        img_width = max(400, min(max_line_len * font_size // 2 + padding * 2, 1200))
        img_height = max(100, len(lines) * line_height + padding * 2)
        img = Image.new("RGB", (img_width, img_height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        font = _load_render_font(ImageFont, font_size)
        y = padding
        for line in lines[:200]:  # cap at 200 lines
            draw.text((padding, y), line[:200], fill=(0, 0, 0), font=font)
            y += line_height
        img.save(img_path.as_posix(), "PNG")
        logger.info(f"[distribute] rendered essence text to image: {img_path.name}")
        return img_path

    # ---------- Netdisk distribution (kind=netdisk) ----------

    async def distribute_netdisk(
        self,
        path: str,
        target: str,
        *,
        group_id: str = "",
        name: str = "",
        convert_to: str = "",
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
            # Optional conversion argument is only forwarded when requested,
            # keeping older Ingest adapters (tests/plugins) compatible; album
            # media is always lossy re-encoded (mandatory built-in).
            extra = {}
            if convert_to:
                extra["convert_to"] = convert_to
            tid = await self.ingest.submit_fetch(
                group_id or "0",
                link.url,
                name=name or path_basename(path),
                to_album=(target == "album"),
                to_essence=to_essence,
                album_name="AstrBot云盘",
                **extra,
            )
            return {"target": target, "task_id": tid, "via": "netdisk-link"}
        raise ValueError(f"unsupported target for netdisk: {target}")