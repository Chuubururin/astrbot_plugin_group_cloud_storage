"""NapCat 能力混入 —— 按 ports/capabilities 分类实现各接口（JSON → DTO）。

NapCat 返回字段（实测）：
- get_group_root_files / get_group_files_by_folder →
  {files:[{file_id, file_name, file_size, busid, uploader, uploader_name,
  upload_time, modify_time, folder_id?}], folders:[{folder_id, folder_name}]}
- get_group_file_system_info → {file_count, limit_count, used_space, total_space}
- get_group_file_url → url 字符串或 {url}
- get_qun_album_list → {album_list:[{album_id, name, owner, desc, create_time,
  upload_number, ...}], has_more}
- get_essence_msg_list → [{message_id, msg_seq, sender_id, sender_nick,
  content:[{type, data:{text,...}}], ...}]
"""

from __future__ import annotations

from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.domain.resource import (
    FileSystemInfo,
    GroupFile,
    GroupFileList,
    GroupFolder,
    GroupMember,
)


class NapCatCoreMixin:
    """核心能力：登录信息、群列表、群消息发送。"""

    async def get_login_info(self) -> dict:
        return await self._call("get_login_info") or {}

    async def list_groups(self) -> list[dict]:
        data = await self._call("get_group_list")
        return list(data or [])

    async def send_group_msg(self, group_id: str, message: list) -> dict:
        return (
            await self._call("send_group_msg", group_id=group_id, message=message) or {}
        )


class NapCatGroupMixin:
    """群组信息查询：群详情、成员列表。"""

    async def get_group_info(self, group_id: str) -> dict:
        return await self._call("get_group_info", group_id=group_id) or {}

    async def list_group_members(self, group_id: str) -> list[GroupMember]:
        data = await self._call("get_group_member_list", group_id=group_id)
        return [
            GroupMember(
                user_id=str(m.get("user_id", "")),
                nickname=m.get("nickname") or m.get("card") or "",
                role=m.get("role", ""),
            )
            for m in (data or [])
        ]

    async def get_group_member_info(self, group_id: str, user_id: str) -> dict:
        return (
            await self._call(
                "get_group_member_info", group_id=group_id, user_id=user_id
            )
            or {}
        )

    async def set_group_name(self, group_id: str, name: str) -> None:
        await self._call("set_group_name", group_id=group_id, group_name=name)

    async def get_essence_msg_list(self, group_id: str) -> list:
        data = await self._call("get_essence_msg_list", group_id=group_id) or {}
        return list(data if isinstance(data, list) else data.get("data") or [])

    async def set_essence_msg(self, message_id: str) -> None:
        await self._call("set_essence_msg", message_id=message_id)

    async def delete_essence_msg(self, message_id: str) -> None:
        await self._call("delete_essence_msg", message_id=message_id)


class NapCatGroupExtendsMixin:
    """群扩展操作：加群选项、群备注、群相册图片上传。"""

    async def set_group_add_option(self, group_id: str, add_type: int) -> None:
        await self._call("set_group_add_option", group_id=group_id, add_type=add_type)

    async def set_group_remark(self, group_id: str, remark: str) -> None:
        await self._call("set_group_remark", group_id=group_id, remark=remark)

    async def upload_image_to_qun_album(
        self, group_id: str, album_id: str, album_name: str, file: str
    ) -> None:
        await self._call(
            "upload_image_to_qun_album",
            group_id=group_id,
            album_id=album_id,
            album_name=album_name,
            file=file,
        )


class NapCatFileMixin:
    """群文件操作：文件列表查询、容量信息、文件直链获取。"""

    @staticmethod
    def _parse_file_list(group_id: str, data: dict) -> GroupFileList:
        files = [
            GroupFile(
                file_id=str(f.get("file_id", "")),
                name=f.get("file_name", ""),
                size=int(f.get("file_size", 0) or 0),
                busid=int(f.get("busid", 0) or 0),
                uploader_id=str(f.get("uploader") or "") or None,
                uploader_name=f.get("uploader_name") or None,
                upload_time=int(f.get("upload_time") or f.get("modify_time") or 0),
                folder_id=str(f.get("folder_id") or "") or None,
            )
            for f in (data.get("files", []) or [])
        ]
        folders = [
            GroupFolder(
                folder_id=str(fl.get("folder_id", "")),
                name=fl.get("folder_name", ""),
            )
            for fl in (data.get("folders", []) or [])
        ]
        return GroupFileList(group_id=group_id, files=files, folders=folders)

    async def list_group_root(self, group_id: str) -> GroupFileList:
        data = await self._call("get_group_root_files", group_id=group_id)
        return self._parse_file_list(group_id, data)

    async def list_group_folder(self, group_id: str, folder_id: str) -> GroupFileList:
        data = await self._call(
            "get_group_files_by_folder", group_id=group_id, folder_id=folder_id
        )
        return self._parse_file_list(group_id, data)

    async def get_group_file_url(
        self, group_id: str, file_id: str, busid: int, name: str
    ) -> str:
        data = await self._call(
            "get_group_file_url", group_id=group_id, file_id=file_id, busid=busid
        )
        if isinstance(data, str):
            return data
        url = (data or {}).get("url")
        if not url:
            raise OneBotApiError(
                OneBotErrorKind.REMOTE_ERROR, "get_group_file_url", "empty url"
            )
        return str(url)

    async def upload_group_file(
        self,
        group_id: str,
        file_path: str,
        name: str,
        folder_id: str | None = None,
    ) -> None:
        params: dict = {"group_id": group_id, "file": file_path, "name": name}
        if folder_id:
            params["folder_id"] = folder_id
        await self._call("upload_group_file", **params)

    async def delete_group_file(self, group_id: str, file_id: str, busid: int) -> None:
        await self._call(
            "delete_group_file", group_id=group_id, file_id=file_id, busid=busid
        )

    async def create_group_file_folder(self, group_id: str, folder_name: str) -> None:
        await self._call(
            "create_group_file_folder", group_id=group_id, folder_name=folder_name
        )

    async def get_group_fs_info(self, group_id: str) -> FileSystemInfo:
        data = await self._call("get_group_file_system_info", group_id=group_id)
        return FileSystemInfo(
            file_count=int(data.get("file_count", 0) or 0),
            limit_count=int(data.get("limit_count", 0) or 0),
            used_space=int(data.get("used_space", 0) or 0),
            total_space=int(data.get("total_space", 0) or 0),
        )


class NapCatGoCqFileMixin:
    """Go-CQHTTP 文件操作：重命名、移动、创建文件夹。"""

    async def rename_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        new_name: str,
    ) -> None:
        await self._call(
            "rename_group_file",
            group_id=group_id,
            file_id=file_id,
            current_parent_directory=current_parent_directory,
            new_name=new_name,
        )

    async def move_group_file(
        self,
        group_id: str,
        file_id: str,
        current_parent_directory: str,
        target_parent_directory: str,
    ) -> None:
        await self._call(
            "move_group_file",
            group_id=group_id,
            file_id=file_id,
            current_parent_directory=current_parent_directory,
            target_parent_directory=target_parent_directory,
        )


class NapCatAlbumMixin:
    """群相册操作：相册列表、相册媒体列表。"""

    async def get_qun_album_list(self, group_id: str) -> list:
        data = await self._call("get_qun_album_list", group_id=group_id) or {}
        return list(data.get("album_list") or [])

    async def get_group_album_media_list(self, group_id: str, album_id: str) -> list:
        data = (
            await self._call(
                "get_group_album_media_list",
                group_id=group_id,
                album_id=album_id,
                attach_info="",
            )
            or {}
        )
        return list(data.get("media_list") or data.get("media") or [])
