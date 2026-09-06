"""OneBot capability protocols, modularized by NapCat API category.

Categories follow the NapCat official API documentation (napcat.apifox.cn):
- Core (core): login info, group list, members, sending messages
- Group (group): group info, group name, essence messages
- Group extends (extends): join options, group remark, album image upload
- File (file): directory listing, direct link, upload, delete, quota, folder creation
- Go-CQHTTP compatible (gocq): group file rename/move

Each protocol declares capabilities only; OneBotApiPort aggregates all
protocols as the single exit point for the business layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.domain.enums import CapabilityState
from core.domain.resource import FileSystemInfo, GroupFileList, GroupMember


class CoreCapability(ABC):
    """Core interface (OneBot standard): identity and messaging."""

    @abstractmethod
    async def get_login_info(self) -> dict:
        """get_login_info: user_id/nickname (owner determination)."""

    @abstractmethod
    async def list_groups(self, no_cache: bool = False) -> list[dict]:
        """Raw get_group_list item (group_id/group_name/member_count...)."""

    @abstractmethod
    async def send_group_msg(self, group_id: str, message: list) -> dict:
        """send_group_msg: returns {message_id,...} (used to mark essence messages)."""


class GroupCapability(ABC):
    """Group interface: info, members, group name, essence."""

    @abstractmethod
    async def get_group_info(self, group_id: str, no_cache: bool = False) -> dict:
        """get_group_info: verify group name consistency after rename."""

    @abstractmethod
    async def list_group_members(self, group_id: str, no_cache: bool = False) -> list[GroupMember]:
        """get_group_member_list: uploader nickname resolution, owner determination."""

    @abstractmethod
    async def get_group_member_info(self, group_id: str, user_id: str, no_cache: bool = False) -> dict:
        """get_group_member_info: lightweight owner check (one user's role only)."""

    @abstractmethod
    async def get_group_honor_info(self, group_id: str, honor_type=None) -> dict:
        """get_group_honor_info: group honor info."""

    @abstractmethod
    async def get_group_system_msg(self, group_id: str = "", only_pending=False, count=50) -> dict:
        """get_group_system_msg: group system messages."""

    @abstractmethod
    async def get_group_info_ex(self, group_id: str, no_cache: bool = False) -> dict:
        """get_group_info_ex: extended profile (SnowLuma/NapCat; richer than get_group_info)."""

    @abstractmethod
    async def get_group_detail_info(self, group_id: str, no_cache: bool = False) -> dict:
        """get_group_detail_info: fuller group profile (SnowLuma only)."""

    @abstractmethod
    async def set_group_name(self, group_id: str, name: str) -> None:
        """set_group_name (requires owner permission)."""

    @abstractmethod
    async def get_essence_msg_list(self, group_id: str) -> list:
        """get_essence_msg_list: essence message list (resource stats / full-text rebuild)."""

    @abstractmethod
    async def set_essence_msg(self, message_id: str) -> None:
        """set_essence_msg (requires admin permission)."""

    @abstractmethod
    async def delete_essence_msg(self, message_id: str) -> None:
        """delete_essence_msg (requires admin permission)."""


class GroupExtendsCapability(ABC):
    """Group extension APIs (NapCat): join options, group remark, album image upload."""

    @abstractmethod
    async def set_group_add_option(self, group_id: str, add_type: int) -> None:
        """set_group_add_option: 1=allow, 2=verify, 3=forbid, 4=question, 5=question+review."""

    @abstractmethod
    async def set_group_remark(self, group_id: str, remark: str) -> None:
        """set_group_remark: this account's remark for the group."""

    @abstractmethod
    async def upload_image_to_qun_album(
        self, group_id: str, album_id: str, album_name: str, file: str
    ) -> None:
        """upload_image_to_qun_album: file is a local path."""


class FileCapability(ABC):
    """File interface: directory listing, direct link, upload, delete, quota, folder creation."""

    @abstractmethod
    async def list_group_root(self, group_id: str) -> GroupFileList:
        """get_group_root_files: files and folders in the root directory."""

    @abstractmethod
    async def list_group_folder(self, group_id: str, folder_id: str = "", folder: str = "") -> GroupFileList:
        """get_group_files_by_folder: list a specific folder."""

    @abstractmethod
    async def get_group_file_url(
        self, group_id: str, file_id: str, busid: int | None = None, name: str = ""
    ) -> str:
        """get_group_file_url: on-demand download direct link (not persisted)."""

    @abstractmethod
    async def upload_group_file(
        self,
        group_id: str,
        file_path: str,
        name: str = "",
        folder_id: str | None = None,
        folder: str = "",
        upload_file: bool = True,
    ) -> None:
        """upload_group_file: uploads a local path; caller splits into volumes above 95MB."""

    @abstractmethod
    async def delete_group_file(self, group_id: str, file_id: str, busid: int | None = None) -> None:
        """delete_group_file."""

    @abstractmethod
    async def create_group_file_folder(self, group_id: str, folder_name: str, parent_id: str = "/") -> None:
        """create_group_file_folder (requires owner/admin permission)."""

    @abstractmethod
    async def delete_group_file_folder(self, group_id: str, folder_id: str) -> None:
        """delete_group_file_folder."""

    @abstractmethod
    async def rename_group_file_folder(self, group_id: str, folder_id: str, new_name: str) -> None:
        """rename_group_file_folder."""

    @abstractmethod
    async def get_group_fs_info(self, group_id: str) -> FileSystemInfo:
        """get_group_file_system_info: quota info.

        NapCat used_space is always 0; usage falls back to the indexed SUM.
        """

    @abstractmethod
    async def download_file(self, url: str = "", base64: str = "", name: str = "") -> dict:
        """download_file: download a file to data/downloads (SnowLuma)."""

    @abstractmethod
    async def get_file(self, file_id: str = "", file: str = "") -> dict:
        """get_file: get cached file info (image/voice, not group files)."""

    @abstractmethod
    async def get_image(self, file: str = "", file_id: str = "") -> dict:
        """get_image: get cached image info."""

    @abstractmethod
    async def trans_group_file(self, group_id: str, file_id: str, target_group_id: str) -> dict:
        """trans_group_file: cross-group file transfer (NapCat, avoids extra disk writes)."""

    @abstractmethod
    async def persist_group_file(self, group_id: str, file_id: str) -> dict:
        """set_group_file_forever / persist_group_file: keep a group file forever (LLBot/Milly)."""


class GoCqFileCapability(ABC):
    """Go-CQHTTP compatibility: group file rename/move (NapCat contract parameters)."""

    @abstractmethod
    async def rename_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        new_name: str,
    ) -> None:
        """rename_group_file."""

    @abstractmethod
    async def move_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        target_parent_directory: str,
    ) -> None:
        """move_group_file."""


class AlbumCapability(ABC):
    """Group album interface: album and media lists (on demand; cloud is the source)."""

    @abstractmethod
    async def get_qun_album_list(self, group_id: str) -> list:
        """get_qun_album_list: album list (resource stats)."""

    @abstractmethod
    async def create_group_album(self, group_id: str, album_name: str, album_desc: str = "") -> dict:
        """create_group_album: OneBot extension template; not implemented by SnowLuma."""

    @abstractmethod
    async def get_group_album_list(self, group_id: str) -> list:
        """get_group_album_list: group album list."""

    @abstractmethod
    async def delete_group_album_media(self, group_id: str, album_id: str, lloc: str) -> None:
        """del_group_album_media."""

    @abstractmethod
    async def comment_group_album_media(self, group_id: str, album_id: str, lloc: str, content: str) -> None:
        """do_group_album_comment."""

    @abstractmethod
    async def like_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        """set_group_album_media_like."""

    @abstractmethod
    async def unlike_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        """cancel_group_album_media_like."""

    @abstractmethod
    async def delete_group_album(self, group_id: str, album_id: str) -> dict:
        """delete_group_album: delete an entire album (LLBot; not implemented by SnowLuma)."""

    @abstractmethod
    async def get_group_album_media_list(self, group_id: str, album_id: str) -> list:
        """get_group_album_media_list: album media list (real time)."""
