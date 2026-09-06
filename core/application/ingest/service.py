from __future__ import annotations

from pathlib import Path

from core.application.queue import OpQueue
from core.application.sync import ResourceSyncService
from core.application.transfer import FETCH_MAX_BYTES, FETCH_TIMEOUT_SEC
from core.application.composition.splitter import ESSENCE_CHUNK_MAX_CHARS
from core.config import PluginConfig
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

from .essence import EssenceMixin
from .video import VideoMixin, VIDEO_SEGMENT_MAX_SECONDS
from .album import AlbumMixin
from .fetch import FetchMixin
from .context import IngestContext


class CloudIngestService(EssenceMixin, VideoMixin, AlbumMixin, FetchMixin):
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        sync: ResourceSyncService,
        tmp_dir: Path,
        config: dict | None = None,
        transfer=None,
        converter=None,
    ):
        # Config object injection: unified PluginConfig boundary (dicts pass
        # through for compatibility, see core.config.model)
        cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
        self.context = IngestContext(
            api=api,
            store=store,
            queue=queue,
            sync=sync,
            tmp_dir=Path(tmp_dir),
            transfer=transfer,
            converter=converter,
            essence_chunk_chars=int(cfg.get("essence_chunk_size", ESSENCE_CHUNK_MAX_CHARS) or ESSENCE_CHUNK_MAX_CHARS),
            video_segment_seconds=int(cfg.get("video_segment_seconds", VIDEO_SEGMENT_MAX_SECONDS) or VIDEO_SEGMENT_MAX_SECONDS),
            fetch_max_bytes=int(cfg.get("fetch_max_bytes", FETCH_MAX_BYTES) or FETCH_MAX_BYTES),
            fetch_timeout=float(cfg.get("fetch_timeout_sec", FETCH_TIMEOUT_SEC) or FETCH_TIMEOUT_SEC),
        )
        self.context.tmp_dir.mkdir(parents=True, exist_ok=True)
        # Handler mixins use these stable aliases; the context remains the source of truth.
        self.api = self.context.api
        self.store = self.context.store
        self.queue = self.context.queue
        self.sync = self.context.sync
        self.transfer = self.context.transfer
        self.converter = self.context.converter
        self.tmp_dir = self.context.tmp_dir
        self.essence_chunk_chars = self.context.essence_chunk_chars
        self.video_segment_seconds = self.context.video_segment_seconds
        self.fetch_max_bytes = self.context.fetch_max_bytes
        self.fetch_timeout = self.context.fetch_timeout
        self._sync_locks = self.context.sync_locks

    async def handle(self, op) -> None:
        if op.kind == "essence_save":
            await self._do_essence_save(op)
        elif op.kind == "essence_delete":
            await self._do_essence_delete(op)
        elif op.kind == "fetch":
            await self._do_fetch(op)
        elif op.kind == "video_upload":
            await self._do_video_upload(op)
        elif op.kind == "video_album":
            await self._do_video_album(op)
        elif op.kind == "image_album":
            await self._do_image_album(op)
        else:
            raise ValueError(f"unknown ingest op kind: {op.kind}")
