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

The pure computation inside these flows (album media name matching / spec
ranking, text->image rendering) lives in ``core.application.distribution``;
this module keeps only IO orchestration.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Protocol

from core.application.common import path_basename
from core.application.distribution import media_spec, text_render
from core.log import logger
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

# Distribution target whitelist
DISTRIBUTE_TARGETS = frozenset(
    {"local", "netdisk", "album", "essence", "group", "copy"}
)


class BridgePort(Protocol):
    """DistributorService 依赖的 BridgeService 公开面（P1b）。

    分发只允许走这四个方法：离线下载的目标布局（{group_id}/{filename}
    模板 + 幂等 mkdir）是 bridge 的内部约定，不得在分发侧复渲染。
    """

    async def submit_out(
        self,
        group_id: str,
        resource_id: int,
        *,
        dst_dir: str | None = None,
        force: bool = False,
    ) -> str: ...

    async def submit_in(self, path: str, *, group_id: str) -> str: ...

    async def submit_offline(self, url: str, group_id: str, filename: str) -> str: ...

    async def get_raw_url(self, path: str): ...


class DistributorService:
    def __init__(
        self,
        store: MetaStorePort,
        api: OneBotApiPort,
        *,
        ops=None,
        bridge: BridgePort | None = None,
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
            # 坏链#18：QQ CDN 直链的 URL 尾段是规格名（/0 /400 /800…），
            # OpenList 离线下载按「URL 尾段，无 Content-Disposition 才用」
            # 命名（AlistGo internal/offline_download/http/client.go 同款
            # 语义），落盘名会变成 "800"。经 dlserver 代理重发，由
            # Content-Disposition 注入真实文件名。
            if self.dlserver and getattr(self.dlserver, "enabled", False):
                proxied = self.dlserver.register_proxy(
                    url, name or "album_media",
                    allow_private=getattr(self.dlserver, "allow_private", False),
                )
                if proxied:
                    url = proxied
            else:
                # 无代理时至少让落盘可辨识：URL 尾段太短/纯数字时追加名字
                from urllib.parse import urlsplit

                tail = urlsplit(url).path.rsplit("/", 1)[-1]
                if not tail or tail.isdigit():
                    url = f"{url}{'' if url.endswith('/') else '/'}{name or 'album_media'}"
            # 坏链#17：{group_id}/{filename} 落盘模板渲染 + 幂等 mkdir 的
            # 存储布局约定收敛在 bridge.submit_offline（与 bridge_out 同源）。
            tid = await self.bridge.submit_offline(url, group_id, name or "album_media")
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
            media_type = (
                "视频" if ext in media_spec.VIDEO_EXTS else "图片"
            )
            title = f"[{media_type}] {name}" if name else f"[{media_type}] album_{album_id}"
            text = f"来源：群相册\n媒体类型：{media_type}\n文件名：{name}\n直链：{url}"
            tid = await self.ingest.submit_essence_save(group_id, title, text)
            return {"target": "essence", "task_id": tid, "via": "media-meta"}
        raise ValueError(f"unsupported target for album: {target}")

    async def _album_media_url(self, group_id: str, album_id: str, name: str) -> str:
        """Album media direct link: match by filename first; fall back to the
        first entry. Matching + spec ranking live in distribution.media_spec."""
        media = await self.api.get_group_album_media_list(group_id, album_id)
        if not media:
            raise ValueError("album media list empty")
        return media_spec.select_media_url(media, name)

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
            if self.dlserver and getattr(self.dlserver, "enabled", False) and self.tmp_dir:
                staged = self._stage_text(text, group_id, rid)
                addr = self.dlserver.register_staged(staged, served)
                url = addr.get("http_url") or ""
                if not url:
                    raise ValueError("download server returned no http url")
                # {group_id}/{filename} 落盘模板 + 幂等 mkdir 由 bridge 承担
                tid = await self.bridge.submit_offline(
                    url, group_id, name or f"essence_{rid}"
                )
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
        """Render essence text to a PNG (album entrance: type restriction —
        text becomes an image). Rendering lives in distribution.text_render."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        img_path = (
            self.tmp_dir
            / f"ess_{group_id}_{rid}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        )
        out = text_render.render_text_to_image(text, img_path)
        logger.info(f"[distribute] rendered essence text to image: {out.name}")
        return out

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
            link = await self.bridge.get_raw_url(path)
            return {"target": "local", "http_url": link.url}
        if target == "group":
            if not self.bridge or not group_id:
                raise ValueError("bridge and group_id required")
            tid = await self.bridge.submit_in(path, group_id=group_id)
            return {"target": "group", "task_id": tid}
        if target in ("album", "essence"):
            if not self.bridge or not self.ingest:
                raise ValueError("bridge / ingest required")
            link = await self.bridge.get_raw_url(path)
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
