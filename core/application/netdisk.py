"""NetdiskService -- OpenList netdisk browse/register/index/tag
orchestration.

Behavior tiers:
- browse registration: request-triggered convergence; idempotent
  registration of the current directory entries, no resident tasks
- deep_index: manual task (WebUI/task triggered; OpQueue kind=netdisk_index,
  directory-granularity throttling, cancellable; SSE progress
  {task_id, kind:"netdisk_index", i, n})

Dependencies: OpenListClient (the single OpenList egress point),
MetaStorePort (netdisk_meta), PluginConfig (type_ext_overrides), OpQueue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.domain.file_type import classify_with_overrides
from core.application.common import utc_now_iso as _now
from core.log import logger
from ports.meta_store import MetaStorePort

if TYPE_CHECKING:
    from adapters.external.openlist import OpenListClient
    from core.config import PluginConfig
    from core.application.queue import OpQueue


def _dir_prefix(path: str) -> str:
    p = "/" + (path or "/").strip("/")
    return p if p.endswith("/") else p + "/"


class NetdiskService:
    """Netdisk-side orchestration: browse registration / deep index / tags /
    direct links."""

    def __init__(
        self,
        client: "OpenListClient",
        store: MetaStorePort,
        config: "PluginConfig",
        queue: "OpQueue",
    ):
        self._client = client
        self._store = store
        self._config = config
        self._queue = queue

    # ---------- Browse + registration (request-triggered convergence) ----------

    async def browse(self, path: str, page: int = 1, per_page: int = 50) -> dict:
        """List a directory page; on success idempotently register its
        entries and merge tags."""
        files, has_more = await self._client.list_dir_page(path, page, per_page)

        ext_overrides = self._config.get("type_ext_overrides") or {}
        prefix = _dir_prefix(path)
        rows = []
        for f in files:
            remote_path = f"{prefix}{f.name}" if not f.is_dir else f"{prefix}{f.name}/"
            rows.append(
                {
                    "remote_path": remote_path,
                    "name": f.name,
                    "is_dir": 1 if f.is_dir else 0,
                    "size": int(f.size or 0),
                    "type": "folder"
                    if f.is_dir
                    else classify_with_overrides(f.name, ext_overrides),
                    "tags": "",
                    "registered_at": _now(),
                }
            )
        await self._store.upsert_netdisk_rows(rows)

        metas = {
            m["remote_path"]: m for m in await self._store.get_netdisk_meta(prefix)
        }
        items = []
        for f, row in zip(files, rows):
            meta = metas.get(row["remote_path"], {})
            items.append(
                {
                    "name": f.name,
                    "is_dir": bool(f.is_dir),
                    "size": int(f.size or 0),
                    "modified": f.modified,
                    "remote_path": row["remote_path"],
                    "type": meta.get("type") or row["type"],
                    "tags": meta.get("tags") or "",
                    "indexed_at": meta.get("indexed_at") or "",
                }
            )
        return {
            "path": path,
            "items": items,
            "page": page,
            "page_size": per_page,
            "has_more": has_more,
        }

    # ---------- Tags ----------

    async def set_tags(self, remote_path: str, tags: list[str]) -> None:
        await self._store.set_netdisk_tags(remote_path, ",".join(tags))

    # ---------- Direct links ----------

    async def direct_link(self, remote_path: str) -> str:
        link = await self._client.get_raw_url(remote_path)
        return link.url

    # ---------- Deep index (manual task) ----------

    async def submit_index(self, path: str) -> str:
        """Submit a deep index task (manual trigger)."""
        return await self._queue.submit(
            "netdisk_index", target=path, payload={"path": path}
        )

    async def handle_index(self, op) -> None:
        """Recursively register + backfill indexed_at; page by page per
        directory, SSE progress."""
        root = _dir_prefix(op.payload["path"])
        pending = [root]
        seen = 0
        ext_overrides = self._config.get("type_ext_overrides") or {}
        try:
            while pending:
                dir_path = pending.pop(0)
                page = 1
                while True:
                    files, has_more = await self._client.list_dir_page(
                        dir_path, page, 500
                    )
                    rows = []
                    for f in files:
                        remote_path = (
                            f"{dir_path}{f.name}/"
                            if f.is_dir
                            else f"{dir_path}{f.name}"
                        )
                        rows.append(
                            {
                                "remote_path": remote_path,
                                "name": f.name,
                                "is_dir": 1 if f.is_dir else 0,
                                "size": int(f.size or 0),
                                "type": "folder"
                                if f.is_dir
                                else classify_with_overrides(f.name, ext_overrides),
                                "tags": "",
                                "registered_at": _now(),
                            }
                        )
                        if f.is_dir:
                            pending.append(remote_path)
                    if rows:
                        await self._store.upsert_netdisk_rows(rows)
                        await self._store.mark_netdisk_indexed(
                            [r["remote_path"] for r in rows if not r["is_dir"]]
                        )
                    seen += len(rows)
                    self._queue.publish(
                        {
                            "type": "progress",
                            "kind": "netdisk_index",
                            "target": op.target,
                            "task_id": op.task_id,
                            "detail": dir_path,
                            "percent": 0,
                            "i": seen,
                            "n": 0,
                        }
                    )
                    if not has_more or not files:
                        break
                    page += 1
        except Exception as e:
            logger.warning(f"[netdisk] index failed at {root}: {e}")
            raise
        logger.info(f"[netdisk] deep index done: {root} ({seen} entries)")
