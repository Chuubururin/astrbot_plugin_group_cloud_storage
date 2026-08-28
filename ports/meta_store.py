"""MetaStorePort —— 持久化唯一出口（docs/02 §3、docs/04 §2）。

命令层禁止直接操作数据库（DoD #2），一律经由本端口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.domain.sync import (
    GroupInfo,
    VolumeInfo,
    Page,
    ResourceQuery,
    ResourceStats,
    SyncLog,
    SyncResult,
)


class MetaStorePort(ABC):
    """元数据持久化抽象（V1.0 实现：SqliteMetaStore）。"""

    @abstractmethod
    async def upsert_resources(self, items: list) -> int:
        """幂等 UPSERT（DoD #4），返回写入/更新行数。"""

    @abstractmethod
    async def query_resources(self, q: ResourceQuery) -> Page:
        """分页查询资源。"""

    @abstractmethod
    async def get_resource_detail(self, group_id: str, id: int) -> dict | None:
        """详情（限定群范围，防跨群 ID 泄漏，AC10 关联）。"""

    @abstractmethod
    async def update_resource_fields(self, id: int, **fields) -> None:
        """更新资源字段（name/folder_id/status，列名白名单校验；管理操作后同步）。"""

    @abstractmethod
    async def stats(self, group_id: str) -> ResourceStats:
        """单群统计聚合。"""

    @abstractmethod
    async def list_groups(self) -> list[GroupInfo]:
        """群清单（含 role/display_name/sort_order/label，docs/09 §12）。"""

    @abstractmethod
    async def upsert_groups(self, items: list[GroupInfo]) -> int:
        """群信息 upsert（扫描结果写入，group_id 唯一键）。"""

    @abstractmethod
    async def update_group_fields(self, group_id: str, **fields) -> None:
        """更新群管理字段（display_name/label，列名白名单校验）。"""

    @abstractmethod
    async def get_resource_any(self, id: int) -> dict | None:
        """按主键全局取资源（跨群兜底定位）。"""

    @abstractmethod
    async def count_active(self, group_id: str) -> int:
        """群内 active 文件计数（容量持久化兜底）。"""

    @abstractmethod
    async def upsert_album_essence(
        self, group_id: str, albums: list, essences: list, account_id: str = ""
    ) -> None:
        """资源化：相册条目 + 精华消息（统一资源目录，仅元数据/摘要）。"""

    @abstractmethod
    async def upsert_folders(self, group_id: str, folders: list[dict]) -> None:
        """目录实体持久化（folder_id/folder_name/parent_id 幂等）。"""

    @abstractmethod
    async def list_folders_detail(self, group_id: str) -> list[dict]:
        """群目录实体列表（目录树）。"""

    @abstractmethod
    async def clear_folders(self, group_id: str) -> None:
        """清空群目录（全量刷新前调用）。"""

    @abstractmethod
    async def sum_resource_sizes(self, group_id: str) -> int:
        """已用容量（索引精确统计）：群内 active 文件大小合计。"""

    @abstractmethod
    async def set_groups_managed(self, group_ids: list[str], managed: int) -> None:
        """批量设置管理标记（0=从管理列表移除且扫描不复活）。"""

    @abstractmethod
    async def reorder_groups(self, ordered_ids: list[str]) -> None:
        """按传入顺序持久化 sort_order。"""

    @abstractmethod
    async def get_resource_by_resource_id(self, resource_id: str) -> dict | None:
        """按唯一键取资源（分卷父资源/回填用）。"""

    @abstractmethod
    async def insert_volumes(self, items: list[VolumeInfo]) -> None:
        """分卷注册（docs/09 §14.1，父资源下 seq 唯一）。"""

    @abstractmethod
    async def list_volumes(self, parent_resource_id: str) -> list[VolumeInfo]:
        """按序返回父资源的分卷列表。"""

    @abstractmethod
    async def update_volume_fields(
        self, parent_resource_id: str, seq: int, **fields
    ) -> None:
        """更新分卷字段（source_ref/busid/sha256/status，白名单校验）。"""

    @abstractmethod
    async def backfill_volume_by_part(
        self, group_id: str, part_name: str, source_ref: str, busid: int
    ) -> int:
        """事件驱动回填：按 part 文件名匹配同群未就绪卷，返回回填条数。"""

    @abstractmethod
    async def remove_volumes(self, parent_resource_id: str) -> None:
        """删除父资源的全部分卷（级联清理）。"""

    @abstractmethod
    async def mark_missing_as_deleted(
        self, group_id: str, complete: bool, source_file_ids: set[str]
    ) -> int:
        """孤儿清理：仅当 complete=True 时执行（DoD #5 / AC9），返回置 deleted 行数。"""

    @abstractmethod
    async def create_sync_log(self, log: SyncLog) -> int:
        """创建同步日志，返回任务号。"""

    @abstractmethod
    async def finish_sync_log(self, log_id: int, result: SyncResult) -> None:
        """结束同步日志。"""

    @abstractmethod
    async def save_snapshot(self, snap) -> None:
        """保存统计快照（只追加）。"""

    @abstractmethod
    async def fts_match(
        self, group_id: str | None, q: str, limit: int = 2000
    ) -> list[int]:
        """磁盘化全文检索（FTS5 trigram；短词元回退 name LIKE）。"""

    @abstractmethod
    async def mark_all_groups_managed(self, managed: int) -> int:
        """批量设置所有群的管理标记（启动时 managed=0 保护，扫描后恢复）。"""

    @abstractmethod
    async def mark_account_groups_managed(self, account_id: str, managed: int) -> int:
        """按账号 ID 批量设置群管理标记（0=账号离线后隐藏，1=恢复）。"""

    @abstractmethod
    async def restore_account_groups(self, account_id: str) -> int:
        """账号恢复在线：将该账号的群 managed 重置为 1（扫描成功后调用）。"""

    # ---------- archive_map (REQ-03/09) ----------

    @abstractmethod
    async def get_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> dict | None:
        """Get archive map entry for a specific resource and direction."""

    @abstractmethod
    async def upsert_archive_map(self, row: dict) -> None:
        """Insert or update archive map entry."""

    @abstractmethod
    async def clear_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> None:
        """Remove archive map entry for a specific resource and direction."""

    @abstractmethod
    async def list_archive_map(
        self, *, states: tuple[str, ...], direction: str
    ) -> list[dict]:
        """List archive map entries filtered by state and direction."""

    @abstractmethod
    async def update_archive_state(self, row: dict, state: str) -> None:
        """Update state of an archive map entry."""

    @abstractmethod
    async def list_archived_done_ids(
        self, resource_ids: list[int], direction: str = "out"
    ) -> set[int]:
        """2026-09-01 N-02：返回给定资源 ID 中已归档完成（state=done）的 ID 集
        （文件状态筛选「在网盘」派生），供列表投影批量判定。"""

    @abstractmethod
    async def update_archive_state_by_task(self, task_id: str, state: str) -> None:
        """Update state of an archive map entry by task_id."""

    @abstractmethod
    async def update_archive_remote_path(
        self, resource_id: int, group_id: str, direction: str, new_remote_path: str
    ) -> None:
        """Update remote_path of an archive map entry (for rename operations)."""

    @abstractmethod
    async def get_archive_map_by_task(self, task_id: str) -> dict | None:
        """Get archive map entry by bridge task id (OpenList task or fetch op id)."""

    # ---------- netdisk_meta（ADR-0004，N4 网盘索引与标记） ----------

    @abstractmethod
    async def upsert_netdisk_rows(self, rows: list[dict]) -> int:
        """浏览登记：幂等新增（INSERT OR IGNORE，不覆盖已有人工标注）；返回新增行数。"""

    @abstractmethod
    async def get_netdisk_meta(self, dir_prefix: str) -> list[dict]:
        """按目录前缀取标记行（remote_path LIKE dir_prefix%）。"""

    @abstractmethod
    async def set_netdisk_tags(self, remote_path: str, tags: str) -> None:
        """设置单文件标签（覆盖式）。"""

    @abstractmethod
    async def mark_netdisk_indexed(self, remote_paths: list[str]) -> None:
        """深度索引回填 indexed_at。"""

    # ---------- 任务台账与操作流（v15，ADR-0005 经纠偏 D-6 实施） ----------

    @abstractmethod
    async def ledger_upsert(
        self,
        task_id: str,
        kind: str,
        target: str = "",
        payload: dict | None = None,
        state: str = "pending",
        retries: int = 0,
        error: str | None = None,
    ) -> None:
        """任务台账 upsert（状态机：pending/running/paused/retry/done/failed/cancelled）。"""

    @abstractmethod
    async def ledger_get(self, task_id: str) -> dict | None:
        """按 task_id 取台账行。"""

    @abstractmethod
    async def ledger_query(
        self,
        state: str | None = None,
        kind: str | None = None,
        target: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """台账分页查询（updated_at 倒序）。"""

    @abstractmethod
    async def ledger_reconcile(self) -> int:
        """启动对账：白名单 kind 置 pending（断点续传候选），其余置 failed。"""

    @abstractmethod
    async def ops_append(
        self, task_id: str, action: str, before: dict | None, after: dict | None
    ) -> None:
        """操作流追加（可逆操作 before/after 快照；直连操作 task_id 传 ''）。"""

    @abstractmethod
    async def ops_list(self, task_id: str) -> list[dict]:
        """按任务列出操作流（seq 升序）。"""

    @abstractmethod
    async def ops_last_for_resource(self, action: str, resource_id: int) -> dict | None:
        """直连操作定位：按资源取最近一次操作流记录（如标签撤销）。"""

    @abstractmethod
    async def hide_account_groups(self, account_id: str, hidden: int) -> int:
        """账号离线 → 该账号全部群组隐藏（hidden=1 非删除）；恢复在线 → 0。"""

    @abstractmethod
    async def init(self) -> None:
        """建表/迁移。"""

    @abstractmethod
    async def close(self) -> None: ...
