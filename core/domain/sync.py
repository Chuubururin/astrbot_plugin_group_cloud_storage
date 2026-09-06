"""Statistics snapshot and sync task objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import SyncKind, SyncStatus


@dataclass
class Snapshot:
    """Statistics snapshot (append-only, immutable)."""

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
    """Sync task log (written on creation, finished on completion)."""

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
    """Sync completion result (written to sync_logs; gates orphan cleanup)."""

    status: SyncStatus = SyncStatus.OK
    files_found: int = 0
    files_indexed: int = 0
    files_removed: int = 0  # prune-diff removal count (keeps add/remove accounting balanced)
    complete: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == SyncStatus.OK


@dataclass
class ResourceQuery:
    """Resource query criteria (/csfiles paging, /csfind search, Page type filter)."""

    group_id: str = ""
    type: str | None = "file"  # ResourceType.value; None=all types (used by full-text search)
    status: str = "active"
    groups: list[str] | None = None  # cross-group aggregation (IN when set; group_id stays empty)
    keyword: str | None = None
    uploader_id: str | None = None
    folder_id: str | None = None
    exts: list[str] | None = None  # extension filter (with dot, e.g. [".pdf", ".docx"])
    tags: list[str] | None = None  # tag filter (AND, any hit)
    ids: list[int] | None = None  # primary key set filter (rows hit by SearchKV)
    folder: str = ""  # ""=all; "__root__"=root directory; otherwise matches folder_name
    store_status: str = ""  # derived status filter: netdisk/album/essence/none; empty=no filter
    sort_by: str = "created_at"  # sort field whitelist: id/name/size/created_at/uploader_name
    sort_dir: str = "desc"  # asc/desc
    page: int = 1
    page_size: int = 20


@dataclass
class PageItem:
    """Page item: id is the primary key within the current group, used by /csfile <id>."""

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
    type: str = "file"  # ResourceType.value: file/album/essence (unified resource catalog)
    tags: list[str] | None = None  # tags (information organizing)
    path: str = ""  # logical path (filesystem-style addressing)
    ext: str = ""  # file extension (encodable)


@dataclass
class Page:
    """Paged query result."""

    items: list[PageItem]
    total: int
    page: int
    page_size: int


@dataclass
class ResourceStats:
    """Per-group statistics result.

    Attributes:
        group_id: group id
        file_count: number of active files
        total_size: total size of active files
        uploaders: number of uploaders
        by_folder: per-folder stats [{folder_id, folder_name, count, size}]
        by_uploader: per-uploader stats [{uploader_id, uploader_name, count, size}]
        recent_7d: last-7-day stats [{date, count, size}]
        used_space: used space (from OneBot)
        total_space: total space (from OneBot)
        limit_count: file count limit (from OneBot)
    """

    group_id: str
    file_count: int
    total_size: int
    uploaders: int
    # breakdowns
    by_folder: list[dict] = field(default_factory=list)
    by_uploader: list[dict] = field(default_factory=list)
    recent_7d: list[dict] = field(default_factory=list)
    # capacity (from OneBot)
    used_space: int = 0
    total_space: int = 0
    limit_count: int = 0


@dataclass
class GroupInfo:
    """Cached group info (role / display name / sort order / label)."""

    group_id: str
    group_name: str = ""
    join_time: int = 0
    last_sync_at: int = 0
    role: str = "unknown"  # owned / admin / member / unknown
    display_name: str | None = None  # Page display name (may differ from the real group name)
    sort_order: int = 0
    label: str | None = None  # label (A/B/C, 01/02...)
    last_scan_at: int | None = None
    used_space: int = 0  # group used capacity (get_group_file_system_info; cross-group stats)
    total_space: int = 0  # group capacity limit (about 10GB on QQ)
    limit_count: int = 0  # file count limit (get_group_file_system_info)
    file_count: int = 0
    managed: int = 1  # 0=removed from managed list (scans will not restore it)
    album_count: int = 0  # album count (resource stats)
    essence_count: int = 0  # essence message count (resource stats)
    account_id: str = ""  # owning OneBot account (multi-account)
    hidden: int = 0  # 1=hidden while account offline (not deleted; shown again when online)

    @property
    def shown_name(self) -> str:
        """Display name fallback for the Page."""
        return self.display_name or self.group_name or self.group_id


@dataclass
class VolumeInfo:
    """Volume mapping (WinRAR-style volume mode)."""

    parent_resource_id: str
    seq: int
    part_name: str
    source_ref: str | None = None
    busid: int | None = None
    size: int = 0
    sha256: str | None = None
    status: str = "pending"  # pending / uploading / uploaded / failed
    upload_time: int | None = None
    group_id: str | None = None  # group where the volume lives (cross-group storage)


@dataclass
class ScanResult:
    """Statistics snapshot of one group scan (GroupScanService.last_result)."""

    total: int = 0
    owned: int = 0
    scanned_at: int = 0
    failed: int = 0  # groups whose API call failed in this scan

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "owned": self.owned,
            "scanned_at": self.scanned_at,
            "failed": self.failed,
        }
