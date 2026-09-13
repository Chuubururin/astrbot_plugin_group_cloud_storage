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
- essence -> netdisk = full text staged -> OpenList offline download
  (pulled from the local download server; without one: staged into
  group files only, the netdisk transfer stays a manual bridge action)
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
import uuid
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
        # Strong refs for fire-and-forget tasks: asyncio keeps only weak
        # references to running tasks, so an unstored create_task() result
        # can be garbage-collected mid-flight (CPython-documented footgun).
        self._bg_tasks: set = set()

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
                task = _aio.get_running_loop().create_task(
                    self.dlserver.ensure_local(group_id, rid, name)
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
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
            # 坏链#17：目录 = openlist_dst_dir + {group_id}/{filename} 模板
            # 渲染（与 bridge_out 的 _render_dst 同源）。此前只传裸 _dst_dir，
            # 媒体全部堆在挂载根目录，违背按群归档的落盘约定。
            client = self._bridge_client(bridge=self.bridge)
            remote_dir, _remote_path = self.bridge._render_dst(
                getattr(self.bridge, "_dst_dir", "") or "/",
                group_id,
                name or "album_media",
            )
            # 坏链#18：QQ CDN 直链的 URL 尾段是规格名（/0 /400 /800…），
            # OpenList 离线下载按「URL 尾段，无 Content-Disposition 才用」
            # 命名（AlistGo internal/offline_download/http/client.go 同款
            # 语义），落盘名会变成 "800"。经 dlserver 代理重发，由
            # Content-Disposition 注入真实文件名。
            if self.dlserver and getattr(self.dlserver, "enabled", False):
                proxied = self.dlserver.register_proxy(url, name or "album_media")
                if proxied:
                    url = proxied
            else:
                # 无代理时至少让落盘可辨识：URL 尾段太短/纯数字时追加名字
                from urllib.parse import urlsplit

                tail = urlsplit(url).path.rsplit("/", 1)[-1]
                if not tail or tail.isdigit():
                    url = f"{url}{'' if url.endswith('/') else '/'}{name or 'album_media'}"
            await client.mkdir(remote_dir)
            tasks = await client.submit_offline_download([url], remote_dir)
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
            # QQ album entries often drop the file extension in image.name
            # (live 2026-09-11: "logo.png" stored as "logo"); compare both
            # forms so the requested file is actually found instead of
            # silently falling back to the first entry (a different photo).
            want_stem = want.rsplit(".", 1)[0] if "." in want else want

            def _forms(value: str) -> tuple:
                v = value.strip().lower()
                stem = v.rsplit(".", 1)[0] if "." in v else v
                return (v, stem)

            for m in media:
                if not isinstance(m, dict):
                    continue
                video = m.get("video") if isinstance(m.get("video"), dict) else {}
                image = m.get("image") if isinstance(m.get("image"), dict) else {}
                video_name = video.get("name") or ""
                # QQ NT entries carry the display name inside image.name;
                # legacy shapes keep it at the top level.
                cand = str(
                    m.get("name")
                    or image.get("name")
                    or m.get("desc")
                    or m.get("filename")
                    or video_name
                ).strip().lower()
                # BUG-16: exact match or filename-prefix match only (avoid
                # false positives from substring containment, e.g. "a" in "data")
                if not cand:
                    continue
                hit = cand == want or cand == want_stem
                if not hit:
                    for c in _forms(cand):
                        if c and (c.startswith(want_stem) or want_stem.startswith(c)):
                            hit = True
                            break
                    if not hit and "." in want:
                        hit = cand.startswith(want) or want.startswith(cand)
                if hit:
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
    def _pick_largest_spec(specs: list) -> dict:
        """Pick the best spec (front-end byAreaDesc semantics, hardened).

        QQ NT album photoUrls/videoUrl lists order small thumbnails first;
        taking the first entry silently degrades distributed media to the
        lowest-resolution variant. Field-proven ranking (live 2026-09-11:
        the same photo is served as two lloc variants — a hi-res one whose
        /800 tail returns 95KB and a lo-res twin whose every tail returns
        2659B; declared width/height was stale on the lo-res variant):
        1. w5*h5 from the URL query — QQ's real pixel size of that variant
        2. declared width*height
        3. URL tail spec — "/0" is QQ's original-image spec (highest),
           then descending numeric tails ("800" > "400" > "200")
        """
        from urllib.parse import parse_qs

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        def rank(p):
            u = (p or {}).get("url") or {}
            if not isinstance(u, dict):
                return (0, 0, 0)
            url = u.get("url") or ""
            real_px = tail_spec = 0
            if isinstance(url, str) and url:
                path, _, query = url.partition("?")
                qs = parse_qs(query.split("#", 1)[0])
                w5 = _int((qs.get("w5") or ["0"])[0])
                h5 = _int((qs.get("h5") or ["0"])[0])
                real_px = w5 * h5
                tail = path.rstrip("/").rsplit("/", 1)[-1]
                try:
                    tail_num = int(tail)
                except ValueError:
                    tail_num = -1
                tail_spec = float("inf") if tail_num == 0 else max(tail_num, 0)
            return (
                real_px,
                tail_spec,
                _int(u.get("width")) * _int(u.get("height")),
            )

        return max(specs or [], key=rank) if specs else {}

    @classmethod
    def _first_spec_url(cls, specs: list) -> str:
        """URL of the largest spec; accept both {url:{url}} and flat url."""
        p = cls._pick_largest_spec(specs)
        u = (p or {}).get("url")
        if isinstance(u, dict) and u.get("url"):
            return str(u["url"])
        if isinstance(u, str) and u:
            return u
        return ""

    @classmethod
    def _extract_media_url(cls, m: dict) -> str:
        """Support both shapes: flat {url} and the nested QQ album image.

        The QQ NT album service returns camelCase media entries
        (image.photoUrls[].url.url plus image.defaultUrl.url); legacy
        NapCat-style adapters return snake_case (image.photo_url). Video
        entries carry videoUrl[]/video_url[] specs and a flat playback url.
        Spec lists resolve to the largest pixel area, mirroring the gallery
        frontend normalization (album-media.js byAreaDesc).
        """
        if not isinstance(m, dict):
            return ""
        flat = m.get("url") or m.get("file") or ""
        if isinstance(flat, str) and flat:
            return flat
        image = m.get("image") or {}
        for key in ("photoUrls", "photo_url"):
            url = cls._first_spec_url(image.get(key) or [])
            if url:
                return url
        default_url = image.get("defaultUrl")
        if isinstance(default_url, dict) and default_url.get("url"):
            return str(default_url["url"])
        video = m.get("video") or {}
        if isinstance(video.get("url"), str) and video["url"]:
            return video["url"]
        for key in ("videoUrl", "video_url"):
            url = cls._first_spec_url(video.get(key) or [])
            if url:
                return url
        cover = video.get("cover") or {}
        for key in ("photoUrls", "photo_url"):
            url = cls._first_spec_url(cover.get(key) or [])
            if url:
                return url
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
            if not self.bridge:
                raise ValueError("bridge not enabled")
            # Preferred path: direct OpenList offline download from the local
            # download server (same mechanism as album media -> netdisk,
            # incl. the {group_id}/{filename} dst template): stage the text,
            # serve it over HTTP, let OpenList pull it. The previous
            # "group relay" submitted only its first hop (upload into the
            # group); the second hop (bridge_out) was never submitted, so
            # the text silently stayed in the group files (live 2026-09-12).
            name = await self._essence_name(group_id, rid)
            # Serve/upload under the essence display name (the staged tmp
            # name embeds a uuid/timestamp which would leak into the
            # netdisk/group file name).
            base = name or f"essence_{rid}"
            served = base if base.endswith(".txt") else f"{base}.txt"
            client = self._bridge_client(bridge=self.bridge)
            remote_dir, _remote_path = self.bridge._render_dst(
                getattr(self.bridge, "_dst_dir", "") or "/",
                group_id,
                name or f"essence_{rid}",
            )
            if self.dlserver and getattr(self.dlserver, "enabled", False) and self.tmp_dir:
                staged = self._stage_text(text, group_id, rid)
                addr = self.dlserver.register_staged(staged, served)
                url = addr.get("http_url") or ""
                if not url:
                    raise ValueError("download server returned no http url")
                await client.mkdir(remote_dir)
                tasks = await client.submit_offline_download([url], remote_dir)
                tid = tasks[0].id if tasks else ""
                return {"target": "netdisk", "task_id": tid, "via": "text-offline"}
            # Fallback (no download server): one hop into the group files;
            # archiving to the netdisk is then a manual bridge action.
            if not self.ops or not self.tmp_dir:
                raise ValueError("ops / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            tid = await self.ops.submit_upload(group_id, staged.as_posix(), served)
            return {
                "target": "netdisk",
                "task_id": tid,
                "via": "group-relay",
                "note": "未配置本地下载服务：文本已上传到群文件，转存网盘请在网盘页手动执行",
            }
        if target == "group":
            if not self.ops or not self.tmp_dir:
                raise ValueError("ops / tmp dir required")
            staged = self._stage_text(text, group_id, rid)
            # Upload under the essence display name (the staged tmp name is
            # unique on disk but would leak its uuid/timestamp into the
            # group file listing — same rationale as the netdisk leg).
            name = await self._essence_name(group_id, rid)
            served = name if name.endswith(".txt") else f"{name}.txt"
            tid = await self.ops.submit_upload(group_id, staged.as_posix(), served)
            return {"target": "group", "task_id": tid}
        if target == "album":
            # Essence text -> album (type restriction at the entrance: text
            # rendered to an image). BUG-15 fix: register the rendered image
            # as a staged file to get an HTTP URL (submit_fetch rejects
            # file:// scheme URLs), then fetch via the download server URL.
            if not self.ingest or not self.tmp_dir:
                raise ValueError("ingest / tmp dir required")
            img_path = await self._render_text_to_image(text, group_id, rid)
            album_name = await self._resolve_essence_album(group_id)
            if not (self.dlserver and self.dlserver.enabled):
                # Fail fast with an actionable message instead of queueing a
                # doomed fetch: submit_fetch only accepts http/https, and
                # without the download server there is no http source for
                # the rendered PNG (the old 127.0.0.1:0 placeholder died
                # later inside the queue with a cryptic error).
                raise ValueError(
                    "本地下载服务未开启：文本转图片需经 HTTP 拉取后入相册，请先启用下载服务"
                )
            staged = self.dlserver.register_staged(img_path, f"精华_{rid}.png")
            url = staged.get("http_url", "")
            tid = await self.ingest.submit_fetch(
                group_id,
                url,
                name=f"精华_{rid}.png",
                to_album=True,
                album_name=album_name,
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
        """Stage the text as a local file and return its path.

        The disk name carries a uuid suffix: the timestamp alone collides
        when the same essence is distributed twice within one second, and
        the colliding overwrite + the upload's post-cleanup unlink kills
        the first registration's staged URL (live 2026-09-12).
        """
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        staged = (
            self.tmp_dir
            / f"ess_{group_id}_{rid}_{int(time.time())}_{uuid.uuid4().hex[:8]}.txt"
        )
        staged.write_text(text, encoding="utf-8")
        return staged

    async def _resolve_essence_album(self, group_id: str) -> str:
        """Album name for essence->album distribution (BUG-8).

        Prefers the dedicated album "AstrBot精华"; when it does not exist in
        the group, falls back to the first existing album instead of failing
        (the protocol side cannot create albums, and upstream fetch has
        always suggested "use an existing album name" as the remedy).
        """
        preferred = "AstrBot精华"
        try:
            albums = await self.api.get_qun_album_list(group_id)
        except Exception as e:
            logger.warning(f"[distribute] album list failed, using default: {e}")
            return preferred
        names = [
            str(a.get("name") or a.get("album_name") or "").strip()
            for a in albums or []
            if isinstance(a, dict)
        ]
        if preferred in names or not names:
            return preferred
        logger.info(
            f"[distribute] album '{preferred}' not in {group_id}, "
            f"falling back to existing album '{names[0]}'"
        )
        return names[0]

    async def _render_text_to_image(self, text: str, group_id: str, rid: int) -> Path:
        """Render essence text to a PNG image (for album import). Pure
        Python + Pillow implementation."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        img_path = (
            self.tmp_dir
            / f"ess_{group_id}_{rid}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        )
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
        # BUG-17: CJK characters are full-width (~font_size per char) while
        # Latin characters are half-width (~font_size/2). Use a weighted
        # estimate: count CJK chars at 1.0x and others at 0.55x of font_size.
        import unicodedata
        def _line_pixel_width(line: str) -> int:
            w = 0
            for ch in line:
                if unicodedata.east_asian_width(ch) in ("W", "F"):
                    w += font_size
                else:
                    w += font_size * 55 // 100
            return w
        max_line_px = max((_line_pixel_width(line) for line in lines), default=200)
        img_width = max(400, min(max_line_px + padding * 2, 1200))
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
            # keeping older Ingest adapters (tests/plugins) compatible. No
            # lossy re-encode here: submit_fetch defaults lossy=False, so
            # re-encoding stays an explicit uploader opt-in at the panel
            # upload path (distribute moves media as-is).
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
