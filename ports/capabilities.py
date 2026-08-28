"""OneBot 能力协议 —— 按 NapCat API 文档分类模块化定义（docs/04 §2、docs/13）。

分类依据：NapCat 官方接口文档（napcat.apifox.cn）的接口归属：
- 核心接口（core）：登录信息/群列表/成员/发消息
- 群组接口（group）：群信息/群名/精华消息
- 群组扩展（extends）：加群方式/群备注/相册图片上传
- 文件接口（file）：目录/直链/上传/删除/容量/建目录
- Go-CQHTTP 兼容（gocq）：重命名/移动群文件

每个协议只声明能力；OneBotApiPort 聚合全部协议作为业务层唯一出口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.domain.enums import CapabilityState
from core.domain.resource import FileSystemInfo, GroupFileList, GroupMember


class CoreCapability(ABC):
    """核心接口（OneBot 标准）：身份与消息。"""

    @abstractmethod
    async def get_login_info(self) -> dict:
        """get_login_info：user_id/nickname（owned 判定）。"""

    @abstractmethod
    async def list_groups(self) -> list[dict]:
        """get_group_list 原始项（group_id/group_name/member_count...）。"""

    @abstractmethod
    async def send_group_msg(self, group_id: str, message: list) -> dict:
        """send_group_msg：返回 {message_id,...}（精华入库载体）。"""


class GroupCapability(ABC):
    """群组接口：信息、成员、群名、精华。"""

    @abstractmethod
    async def get_group_info(self, group_id: str) -> dict:
        """get_group_info：重命名后校验群名一致性。"""

    @abstractmethod
    async def list_group_members(self, group_id: str) -> list[GroupMember]:
        """get_group_member_list：上传者昵称解析、owned 判定。"""

    @abstractmethod
    async def get_group_member_info(self, group_id: str, user_id: str) -> dict:
        """get_group_member_info：owned 轻量判定（仅查自身角色）。"""

    @abstractmethod
    async def set_group_name(self, group_id: str, name: str) -> None:
        """set_group_name（群主权限）。"""

    @abstractmethod
    async def get_essence_msg_list(self, group_id: str) -> list:
        """get_essence_msg_list：精华列表（资源统计/全文重建）。"""

    @abstractmethod
    async def set_essence_msg(self, message_id: str) -> None:
        """set_essence_msg（管理员权限）。"""

    @abstractmethod
    async def delete_essence_msg(self, message_id: str) -> None:
        """delete_essence_msg（管理员权限）。"""


class GroupExtendsCapability(ABC):
    """群组扩展（NapCat）：加群方式、群备注、相册图片写入。"""

    @abstractmethod
    async def set_group_add_option(self, group_id: str, add_type: int) -> None:
        """set_group_add_option：1允许任何人/2需验证/3不允许/4回答问题/5回答+审核。"""

    @abstractmethod
    async def set_group_remark(self, group_id: str, remark: str) -> None:
        """set_group_remark：本账号对群的备注。"""

    @abstractmethod
    async def upload_image_to_qun_album(
        self, group_id: str, album_id: str, album_name: str, file: str
    ) -> None:
        """upload_image_to_qun_album：file=本地路径。"""


class FileCapability(ABC):
    """文件接口：目录浏览、直链、上传、删除、容量、建目录。"""

    @abstractmethod
    async def list_group_root(self, group_id: str) -> GroupFileList:
        """get_group_root_files：根目录文件+文件夹。"""

    @abstractmethod
    async def list_group_folder(self, group_id: str, folder_id: str) -> GroupFileList:
        """get_group_files_by_folder：指定文件夹。"""

    @abstractmethod
    async def get_group_file_url(
        self, group_id: str, file_id: str, busid: int, name: str
    ) -> str:
        """get_group_file_url：实时下载直链（不持久化）。"""

    @abstractmethod
    async def upload_group_file(
        self,
        group_id: str,
        file_path: str,
        name: str,
        folder_id: str | None = None,
    ) -> None:
        """upload_group_file：本地路径上传；>95MB 由调用方分卷。"""

    @abstractmethod
    async def delete_group_file(self, group_id: str, file_id: str, busid: int) -> None:
        """delete_group_file。"""

    @abstractmethod
    async def create_group_file_folder(self, group_id: str, folder_name: str) -> None:
        """create_group_file_folder（群主/管理员权限）。"""

    @abstractmethod
    async def get_group_fs_info(self, group_id: str) -> FileSystemInfo:
        """get_group_file_system_info：容量（NapCat used_space 恒 0，降级索引 SUM）。"""


class GoCqFileCapability(ABC):
    """Go-CQHTTP 兼容：群文件重命名/移动（NapCat 契约参数）。"""

    @abstractmethod
    async def rename_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        new_name: str,
    ) -> None:
        """rename_group_file。"""

    @abstractmethod
    async def move_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        target_parent_directory: str,
    ) -> None:
        """move_group_file。"""


class AlbumCapability(ABC):
    """群相册接口：相册列表与媒体列表（实时按需，云端为源）。"""

    @abstractmethod
    async def get_qun_album_list(self, group_id: str) -> list:
        """get_qun_album_list：相册列表（资源统计）。"""

    @abstractmethod
    async def get_group_album_media_list(self, group_id: str, album_id: str) -> list:
        """get_group_album_media_list：相册媒体列表（实时）。"""
