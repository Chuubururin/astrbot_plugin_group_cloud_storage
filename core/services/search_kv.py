"""SearchKV —— 即时检索索引（名称前缀/子串 + 全文分词）。

内存索引：name_lower -> set(resource 主键)；维护全部事件驱动：
add / remove / 该群 rebuild（文件列表刷新后）。
查询：名称前缀与子串 + 「名称+摘要+标签+群名」全文分词 → 主键集 → SQL IN 分页。
"""

from __future__ import annotations

import asyncio
import bisect

from core.log import logger
from ports.meta_store import MetaStorePort


class SearchKV:
    """即时检索门面（v2.9）：检索下沉到 SQLite FTS5（adapters/store/sqlite.resources_fts）。

    规模目标：百万文件/万群 —— 磁盘索引、查询毫秒级、内存零占用。
    兼容旧调用面（ensure_*/mark_dirty/rebuild 退化为低成本操作）。
    """

    def __init__(self, store: MetaStorePort):
        self.store = store

    async def rebuild_group(self, group_id: str) -> None:
        """无操作（FTS 由数据库触发器维护）。"""

    def add(self, row_id: int, group_id: str, name: str) -> None:
        """无操作（FTS 触发器维护）。"""

    def remove(self, row_id: int) -> None:
        """无操作（FTS 触发器维护）。"""

    def mark_dirty(self, group_id: str) -> None:
        """无操作（FTS 始终与库表一致）。"""

    async def ensure_group(self, group_id: str) -> bool:
        return True

    async def ensure_all(self, group_ids: list[str]) -> None:
        """无操作（FTS 始终就绪）。"""

    async def match_ids(self, group_id: str | None, q: str) -> list[int]:
        """FTS5 检索（trigram 子串 + 短词 LIKE 回退）。"""
        return await self.store.fts_match(group_id, q)

    def stats(self) -> dict:
        return {"backend": "sqlite-fts5-trigram", "memory": 0}
