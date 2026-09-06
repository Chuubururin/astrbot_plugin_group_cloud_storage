"""Domain entities and value objects: Resource / collection DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import ResourceStatus, ResourceType


@dataclass
class GroupFolder:
    """OneBot group file folder (adapter DTO)."""

    folder_id: str
    name: str


@dataclass
class GroupFile:
    """OneBot group file (adapter DTO; raw JSON must not leak upward)."""

    file_id: str
    name: str
    size: int
    busid: int
    uploader_id: str | None = None
    uploader_name: str | None = None
    upload_time: int | None = None
    folder_id: str | None = None
    folder_name: str | None = None


@dataclass
class GroupFileList:
    """Result of one directory listing (files + folders, with per-level completeness)."""

    group_id: str
    files: list[GroupFile] = field(default_factory=list)
    folders: list[GroupFolder] = field(default_factory=list)
    complete: bool = True


@dataclass
class FileSystemInfo:
    """Group file system capacity (get_group_file_system_info result)."""

    file_count: int
    limit_count: int
    used_space: int
    total_space: int


@dataclass
class GroupMember:
    """Group member (used to resolve uploader names)."""

    user_id: str
    nickname: str = ""
    role: str = ""  # owner / admin / member


@dataclass
class Resource:
    """Resource entity (index entry).

    - `id`: internal primary key within the current group (used by /csfile <id>)
    - `resource_id`: `{group_id}:{type}:{source_ref}` idempotent unique key
    """

    group_id: str
    type: ResourceType
    name: str
    source_ref: str
    size: int = 0
    uploader_id: str | None = None
    uploader_name: str | None = None
    busid: int | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    status: ResourceStatus = ResourceStatus.ACTIVE
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    created_at: int = 0
    id: int = 0
    indexed_at: int = 0
    updated_at: int = 0

    @property
    def resource_id(self) -> str:
        return f"{self.group_id}:{self.type.value}:{self.source_ref}"

    @classmethod
    def from_group_file(
        cls, group_id: str, f: GroupFile, uploader_name: str | None = None
    ) -> "Resource":
        return cls(
            group_id=group_id,
            type=ResourceType.FILE,
            name=f.name,
            source_ref=f.file_id,
            size=f.size,
            uploader_id=f.uploader_id,
            uploader_name=uploader_name or f.uploader_name,
            busid=f.busid,
            folder_id=f.folder_id,
            folder_name=f.folder_name,
            created_at=f.upload_time or 0,
        )
