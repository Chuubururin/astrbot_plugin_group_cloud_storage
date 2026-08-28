"""领域实体与值对象：Resource / 采集 DTO（docs/03 §2、docs/02 §3）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import ResourceStatus, ResourceType


@dataclass
class GroupFolder:
    """OneBot 群文件目录（适配器 DTO）。"""

    folder_id: str
    name: str


@dataclass
class GroupFile:
    """OneBot 群文件（适配器 DTO，禁止原始 JSON 上浮，DoD #3）。"""

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
    """一次目录列举的结果（files + folders，含本层是否完整成功）。"""

    group_id: str
    files: list[GroupFile] = field(default_factory=list)
    folders: list[GroupFolder] = field(default_factory=list)
    complete: bool = True


@dataclass
class FileSystemInfo:
    """群文件系统容量（get_group_file_system_info 结果）。"""

    file_count: int
    limit_count: int
    used_space: int
    total_space: int


@dataclass
class GroupMember:
    """群成员（上传者名称解析用）。"""

    user_id: str
    nickname: str = ""
    role: str = ""  # owner / admin / member


@dataclass
class Resource:
    """资源实体（入库对象）。

    - `id`：当前群范围内的内部主键（/csfile <id> 使用）
    - `resource_id`：`{group_id}:{type}:{source_ref}` 幂等唯一键（DoD #4）
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
