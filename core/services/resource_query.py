"""ResourceQueryService / StatsService —— 查询与统计（Slice 3）。

- /csfiles：分页列表（仅 DB 查询，P95 < 2s @1000 files）
- /refile：详情（限定群范围）
- /cssync 报告文本：由本服务汇总示例数据 + 格式化
"""

from __future__ import annotations

import time

from core.domain.sync import Page, ResourceQuery, ResourceStats
from ports.meta_store import MetaStorePort


class ResourceQueryService:
    def __init__(self, store: MetaStorePort):
        self.store = store

    async def page(self, group_id: str, page: int = 1, page_size: int = 20) -> Page:
        return await self.store.query_resources(
            ResourceQuery(group_id=group_id, page=max(page, 1), page_size=page_size)
        )

    async def page_with(self, rq: ResourceQuery) -> Page:
        """透传完整查询条件（keyword/exts 等，Page 检索用）。"""
        return await self.store.query_resources(rq)

    async def detail(self, group_id: str, id: int) -> dict | None:
        return await self.store.get_resource_detail(group_id, id)


class StatsService:
    def __init__(self, store: MetaStorePort):
        self.store = store

    async def stats(self, group_id: str) -> ResourceStats:
        return await self.store.stats(group_id)

    # ---------- 文本渲染 ----------

    @staticmethod
    def format_stats(st: ResourceStats) -> str:
        used = st.used_space / (1024**3) if st.used_space else 0
        total = st.total_space / (1024**3) if st.total_space else 0
        lines = [
            f"📊 群 {st.group_id} 文件统计",
            f"▸ 文件数：{st.file_count}",
            f"▸ 总大小：{StatsService._fmt_size(st.total_size)}",
            f"▸ 上传者：{st.uploaders} 人",
            f"▸ 群容量：{used:.2f} / {total:.2f} GB"
            + (f"（富余 {max(0.0, total - used):.2f} GB）" if total else ""),
        ]
        if st.by_folder:
            lines.append("▸ 目录分布：")
            for it in st.by_folder[:5]:
                lines.append(
                    f"  · {it['folder_name'] or '(根目录)'}：{it['cnt']} 个 / {StatsService._fmt_size(it['bytes'])}"
                )
        if st.by_uploader:
            top = st.by_uploader[0]
            lines.append(
                f"▸ 贡献最多：{top['uploader_name'] or top['uploader_id']}（{top['cnt']} 个）"
            )
        if st.recent_7d:
            lines.append(
                "▸ 近 7 天新增："
                + "、".join(f"{it['d']}:{it['cnt']}" for it in st.recent_7d[:7])
            )
        return "\n".join(lines)

    @staticmethod
    def format_page(page: Page) -> str:
        if not page.items:
            return "（无文件记录，可先 /cssync 触发同步）"
        lines = [f"📂 群文件列表（第 {page.page} 页，共 {page.total} 条）"]
        for it in page.items:
            lines.append(
                f"{it.id}. {it.name}  {StatsService._fmt_size(it.size)}  "
                f"by {it.uploader_name or it.uploader_id or '?'}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_detail(row: dict) -> str:
        created = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(row.get("created_at") or 0)
        )
        return (
            f"📄 {row.get('name')}\n"
            f"▸ ID：{row.get('id')}\n"
            f"▸ 大小：{StatsService._fmt_size(row.get('size') or 0)}\n"
            f"▸ 上传：{row.get('uploader_name') or row.get('uploader_id') or '?'} @ {created}\n"
            f"▸ 目录：{row.get('folder_name') or '(根目录)'}\n"
            f"▸ 状态：{row.get('status')}"
        )

    @staticmethod
    def _fmt_size(n: int) -> str:
        n = n or 0
        if n < 1024:
            return f"{n} B"
        if n < 1024**2:
            return f"{n / 1024:.1f} KB"
        if n < 1024**3:
            return f"{n / 1024**2:.1f} MB"
        return f"{n / 1024**3:.2f} GB"
