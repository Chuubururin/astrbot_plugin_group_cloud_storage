"""NetdiskService -- OpenList 网盘浏览/登记/索引/标记编排（ADR-0004，N4）。

行为分级（HL-14）：
- browse 登记：B 类（用户浏览触发当前目录幂等登记，零常驻任务）
- deep_index：C 类手动（WebUI/任务触发；OpQueue kind=netdisk_index，
  目录粒度限速可取消；SSE 进度 {task_id, kind:"netdisk_index", i, n}）

依赖：OpenListClient（唯一 OpenLog 出口）、MetaStorePort（netdisk_meta）、
PluginConfig（type_ext_overrides，CT-9）、OpQueue。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.domain.file_type import classify_with_overrides
from core.log import logger
from ports.meta_store import MetaStorePort

if TYPE_CHECKING:
    from adapters.external.openlist import OpenListClient
    from core.config import PluginConfig
    from core.services.op_queue import OpQueue


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir_prefix(path: str) -> str:
    p = "/" + (path or "/").strip("/")
    return p if p.endswith("/") else p + "/"


class NetdiskService:
    """网盘侧编排：浏览登记 / 深度索引 / 标签 / 直链。"""

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

    # ---------- 浏览 + 登记（B 类：请求触发收敛） ----------

    async def browse(self, path: str, page: int = 1, per_page: int = 50) -> dict:
        """分页列目录；成功后幂等登记当前目录条目并合并标记。"""
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

    # ---------- 标签 ----------

    async def set_tags(self, remote_path: str, tags: list[str]) -> None:
        await self._store.set_netdisk_tags(remote_path, ",".join(tags))

    # ---------- 直链（REQ-06：仅内存，不持久化） ----------

    async def direct_link(self, remote_path: str) -> str:
        link = await self._client.get_raw_url(remote_path)
        return link.url

    # ---------- 深度索引（C 类手动任务） ----------

    async def submit_index(self, path: str) -> str:
        """提交深度索引任务（手动触发，HL-14 C 类）。"""
        return await self._queue.submit(
            "netdisk_index", target=path, payload={"path": path}
        )

    async def handle_index(self, op) -> None:
        """递归登记 + 回填 indexed_at；目录粒度逐页，SSE 进度。"""
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
