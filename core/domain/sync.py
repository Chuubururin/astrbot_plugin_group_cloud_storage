"""统计快照（docs/03 §3）与同步任务对象（docs/03 §4）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import SyncKind, SyncStatus


@dataclass
class Snapshot:
    """统计快照（只追加，不可变）。"""

    group_id: str
    type: str
    file_count: int
    total_size: int
    used_space: int
    total_space: int
    detail: dict = field(default_factory=dict)
    taken_at: int = 0


@dataclass
class SyncLog:
    """同步任务日志（创建时写入，结束时 finish）。"""

    group_id: str
    kind: SyncKind
    status: SyncStatus = SyncStatus.RUNNING
    files_found: int = 0
    files_indexed: int = 0
    complete: bool = False
    error: str | None = None
    start_at: int = 0
    end_at: int | None = None


@dataclass
class SyncResult:
    """同步结束结果（写入 sync_logs 与驱动孤儿清理门控）。"""

    status: SyncStatus = SyncStatus.OK
    files_found: int = 0
    files_indexed: int = 0
    files_removed: int = 0  # 2026-09-01 D-4：凋零差分剔除条数（增补平衡口径）
    complete: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == SyncStatus.OK


@dataclass
class ResourceQuery:
    """资源查询条件（/csfiles 分页、/csfind 搜索、Page 类型筛选）。"""

    group_id: str = ""
    type: str | None = "file"  # ResourceType.value；None=全类型（v1.2 全文索引用）
    status: str = "active"
    groups: list[str] | None = None  # 跨群聚合（指定时 IN；group_id 为空串配合）
    keyword: str | None = None
    uploader_id: str | None = None
    folder_id: str | None = None
    exts: list[str] | None = None  # 扩展名筛选（含点，如 [".pdf",".docx"]）
    tags: list[str] | None = None  # 标签过滤（AND，任一命中；v1.3 信息整理）
    ids: list[int] | None = None  # 主键集合过滤（SearchKV 命中行）
    folder: str = ""  # ""=全部；"__root__"=根目录；其他=匹配 folder_name
    store_status: str = ""  # 2026-09-01 N-02 派生状态筛选：netdisk/album/essence/none；空=不过滤
    sort_by: str = "created_at"  # 排序字段白名单：id/name/size/created_at/uploader_name（N-07 默认新到旧）
    sort_dir: str = "desc"  # asc/desc（N-07 默认新到旧）
    page: int = 1
    page_size: int = 20


@dataclass
class PageItem:
    """分页包装：id=当前群范围内主键，供 /csfile <id> 使用。"""

    id: int
    resource_id: str
    name: str
    size: int
    uploader_id: str | None
    uploader_name: str | None
    folder_name: str | None
    created_at: int
    indexed_at: int
    busid: int | None
    source_ref: str
    group_id: str = ""
    meta: dict | None = None
    type: str = "file"  # ResourceType.value：file/album/essence（v9 统一资源目录）
    tags: list[str] | None = None  # v1.3 标签（信息整理）
    path: str = ""  # v1.7 逻辑路径（文件系统化寻址）
    ext: str = ""  # v1.7 扩展名（可编码化）


@dataclass
class Page:
    """分页查询结果。"""

    items: list[PageItem]
    total: int
    page: int
    page_size: int


@dataclass
class ResourceStats:
    """单群统计结果。

    Attributes:
        group_id: 群号
        file_count: 活跃文件数
        total_size: 活跃文件总大小
        uploaders: 上传者数量
        by_folder: 按文件夹统计 [{folder_id, folder_name, count, size}]
        by_uploader: 按上传者统计 [{uploader_id, uploader_name, count, size}]
        recent_7d: 近7天统计 [{date, count, size}]
        used_space: 已用空间（来自 OneBot）
        total_space: 总空间（来自 OneBot）
        limit_count: 文件数量上限（来自 OneBot）
    """

    group_id: str
    file_count: int
    total_size: int
    uploaders: int
    # 分类
    by_folder: list[dict] = field(default_factory=list)
    by_uploader: list[dict] = field(default_factory=list)
    recent_7d: list[dict] = field(default_factory=list)
    # 容量（来自 OneBot）
    used_space: int = 0
    total_space: int = 0
    limit_count: int = 0


@dataclass
class GroupInfo:
    """群信息缓存（docs/09 §12.2：角色/展示名/排序/标号）。"""

    group_id: str
    group_name: str = ""
    join_time: int = 0
    last_sync_at: int = 0
    role: str = "unknown"  # owned / admin / member / unknown
    display_name: str | None = None  # Page 展示名（可≠群真实名）
    sort_order: int = 0
    label: str | None = None  # 标号（A/B/C / 01/02…）
    last_scan_at: int | None = None
    used_space: int = 0  # 群容量已用（get_group_file_system_info，跨群统计）
    total_space: int = 0  # 群容量上限（QQ 约 10GB）
    limit_count: int = 0  # 文件数上限（get_group_file_system_info，2026-09-03 v16）
    file_count: int = 0
    managed: int = 1  # 0=已从管理列表移除（扫描不复活）
    album_count: int = 0  # 群相册数量（v8，资源统计）
    essence_count: int = 0  # 精华消息数量（v8，资源统计）
    account_id: str = ""  # 归属 OneBot 账号（v9，多账号）
    hidden: int = 0  # 1=账号离线群组隐藏（v15，D-4 凋零：隐藏非删除，恢复在线后显示）

    @property
    def shown_name(self) -> str:
        """Page 展示名回退逻辑。"""
        return self.display_name or self.group_name or self.group_id


@dataclass
class VolumeInfo:
    """分卷映射（WinRAR 分卷模式，docs/09 §14.1）。"""

    parent_resource_id: str
    seq: int
    part_name: str
    source_ref: str | None = None
    busid: int | None = None
    size: int = 0
    sha256: str | None = None
    status: str = "pending"  # pending / uploading / uploaded / failed
    upload_time: int | None = None
    group_id: str | None = None  # 卷所在群（跨群存储）
