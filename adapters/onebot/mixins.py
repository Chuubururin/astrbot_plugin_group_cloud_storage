"""NapCat capability mixins -- implements each interface grouped by
ports/capabilities (JSON -> DTO).

Observed NapCat response fields:
- get_group_root_files / get_group_files_by_folder ->
  {files: [{file_id, file_name, file_size, busid, uploader, uploader_name,
  upload_time, modify_time, folder_id?}], folders: [{folder_id, folder_name}]}
- get_group_file_system_info -> {file_count, limit_count, used_space, total_space}
- get_group_file_url -> url string or {url}
- get_qun_album_list -> {album_list: [{album_id, name, owner, desc,
  create_time, upload_number, ...}], has_more}
- get_essence_msg_list -> [{message_id, msg_seq, sender_id, sender_nick,
  content: [{type, data: {text, ...}}], ...}]
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
    """Core capabilities: login info, group list, group message sending."""

    async def get_login_info(self) -> dict:
        return await self._call("get_login_info") or {}

    async def list_groups(self, no_cache: bool = False) -> list[dict]:
        data = await self._call("get_group_list", no_cache=no_cache)
        return list(data or [])

    async def send_group_msg(self, group_id: str, message: list) -> dict:
        return (
            await self._call("send_group_msg", group_id=group_id, message=message) or {}
        )


class NapCatGroupMixin:
    """Group info queries: group details and member lists."""

    async def get_group_info(self, group_id: str, no_cache: bool = False) -> dict:
        return await self._call("get_group_info", group_id=group_id, no_cache=no_cache) or {}

    async def list_group_members(self, group_id: str, no_cache: bool = False) -> list[GroupMember]:
        data = await self._call("get_group_member_list", group_id=group_id, no_cache=no_cache)
        return [
            GroupMember(
                user_id=str(m.get("user_id", "")),
                nickname=m.get("nickname") or m.get("card") or "",
                role=m.get("role", ""),
            )
            for m in (data or [])
        ]

    async def get_group_member_info(self, group_id: str, user_id: str, no_cache: bool = False) -> dict:
        return (
            await self._call(
                "get_group_member_info", group_id=group_id, user_id=user_id, no_cache=no_cache
            )
            or {}
        )

    async def get_group_honor_info(self, group_id: str, honor_type=None) -> dict:
        params = {"group_id": group_id}
        if honor_type is not None:
            params["type"] = honor_type
        return await self._call("get_group_honor_info", **params) or {}

    async def get_group_system_msg(self, group_id: str = "", only_pending=False, count=50) -> dict:
        params = {"only_pending": only_pending, "count": count}
        if group_id:
            params["group_id"] = group_id
        return await self._call("get_group_system_msg", **params) or {}

    async def get_group_info_ex(self, group_id: str, no_cache: bool = False) -> dict:
        return await self._call("get_group_info_ex", group_id=group_id, no_cache=no_cache) or {}

    async def get_group_detail_info(self, group_id: str, no_cache: bool = False) -> dict:
        return await self._call("get_group_detail_info", group_id=group_id, no_cache=no_cache) or {}

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
    """Group extension operations: join options, group remark, and group
    album image upload."""

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
    """Group file operations: file listing, capacity info, and direct-link
    retrieval."""

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

    async def list_group_folder(self, group_id: str, folder_id: str = "", folder: str = "") -> GroupFileList:
        params = {"group_id": group_id}
        if folder_id:
            params["folder_id"] = folder_id
        if folder:
            params["folder"] = folder
        data = await self._call("get_group_files_by_folder", **params)
        return self._parse_file_list(group_id, data)

    async def get_group_file_url(
        self, group_id: str, file_id: str, busid: int | None = None, name: str = ""
    ) -> str:
        params = {"group_id": group_id, "file_id": file_id}
        if busid is not None:
            params["busid"] = busid
        data = await self._call("get_group_file_url", **params)
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
        name: str = "",
        folder_id: str | None = None,
        folder: str = "",
        upload_file: bool = True,
    ) -> None:
        params: dict = {
            "group_id": group_id, "file": file_path, "name": name,
            "upload_file": upload_file,
        }
        if folder_id:
            params["folder_id"] = folder_id
        if folder:
            params["folder"] = folder
        await self._call("upload_group_file", **params)

    async def delete_group_file(self, group_id: str, file_id: str, busid: int | None = None) -> None:
        params = {"group_id": group_id, "file_id": file_id}
        if busid is not None:
            params["busid"] = busid
        await self._call("delete_group_file", **params)

    async def create_group_file_folder(self, group_id: str, folder_name: str, parent_id: str = "/") -> None:
        await self._call(
            "create_group_file_folder", group_id=group_id, name=folder_name, parent_id=parent_id
        )

    async def delete_group_file_folder(self, group_id: str, folder_id: str) -> None:
        await self._call("delete_group_file_folder", group_id=group_id, folder_id=folder_id)

    async def rename_group_file_folder(self, group_id: str, folder_id: str, new_name: str) -> None:
        await self._call(
            "rename_group_file_folder", group_id=group_id, folder_id=folder_id,
            new_folder_name=new_name,
        )

    async def get_group_fs_info(self, group_id: str) -> FileSystemInfo:
        data = await self._call("get_group_file_system_info", group_id=group_id)
        return FileSystemInfo(
            file_count=int(data.get("file_count", 0) or 0),
            limit_count=int(data.get("limit_count", 0) or 0),
            used_space=int(data.get("used_space", 0) or 0),
            total_space=int(data.get("total_space", 0) or 0),
        )

    async def download_file(self, url: str = "", base64: str = "", name: str = "") -> dict:
        params: dict = {}
        if url:
            params["url"] = url
        if base64:
            params["base64"] = base64
        if name:
            params["name"] = name
        return await self._call("download_file", **params) or {}

    async def get_file(self, file_id: str = "", file: str = "") -> dict:
        params: dict = {}
        if file_id:
            params["file_id"] = file_id
        if file:
            params["file"] = file
        return await self._call("get_file", **params) or {}

    async def get_image(self, file: str = "", file_id: str = "") -> dict:
        params: dict = {}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return await self._call("get_image", **params) or {}

    async def trans_group_file(self, group_id: str, file_id: str, target_group_id: str) -> dict:
        return await self._call(
            "trans_group_file",
            group_id=group_id,
            file_id=file_id,
            target_group_id=target_group_id,
        ) or {}

    async def persist_group_file(self, group_id: str, file_id: str) -> dict:
        try:
            return await self._call(
                "set_group_file_forever", group_id=group_id, file_id=file_id,
            ) or {}
        except OneBotApiError as e:
            if e.kind != OneBotErrorKind.UNSUPPORTED:
                raise
            return await self._call(
                "persist_group_file", group_id=group_id, file_id=file_id,
            ) or {}


class NapCatGoCqFileMixin:
    """Go-CQHTTP file operations: rename, move, and folder creation."""

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
            parent_directory=current_parent_directory,
            target_directory=target_parent_directory,
        )


class NapCatAlbumMixin:
    """Group album operations: album list and album media list."""

    async def get_qun_album_list(self, group_id: str) -> list:
        data = await self._call("get_qun_album_list", group_id=group_id) or {}
        return list(data.get("album_list") or [])

    async def create_group_album(self, group_id: str, album_name: str, album_desc: str = "") -> dict:
        return await self._call(
            "create_group_album", group_id=group_id,
            name=album_name, desc=album_desc,
        ) or {}

    async def get_group_album_list(self, group_id: str) -> list:
        """Group album list normalized to legacy album_id/name keys.

        Protocol ends differ here: the QQ NT action returns a plain array of
        {id, name, ...} items, NapCat-style ends return an envelope with an
        album_list array of {album_id, album_name, ...} items.
        """
        data = await self._call("get_group_album_list", group_id=group_id)
        items = data if isinstance(data, list) else data or {}
        if not isinstance(items, list):
            items = items.get("album_list") or items.get("albums") or items.get("data") or []
        normalized = []
        for a in items:
            if not isinstance(a, dict):
                continue
            normalized.append(
                {
                    **a,
                    "album_id": str(a.get("album_id") or a.get("id") or ""),
                    "name": str(a.get("name") or a.get("album_name") or ""),
                }
            )
        return normalized

    async def delete_group_album_media(self, group_id: str, album_id: str, lloc: str) -> None:
        await self._call("del_group_album_media", group_id=group_id, album_id=album_id, lloc=lloc)

    async def comment_group_album_media(self, group_id: str, album_id: str, lloc: str, content: str) -> None:
        await self._call("do_group_album_comment", group_id=group_id, album_id=album_id, lloc=lloc, content=content)

    async def like_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        await self._call("set_group_album_media_like", group_id=group_id, album_id=album_id, batch_id=batch_id, lloc=lloc)

    async def unlike_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        await self._call("cancel_group_album_media_like", group_id=group_id, album_id=album_id, batch_id=batch_id, lloc=lloc)

    async def delete_group_album(self, group_id: str, album_id: str) -> dict:
        return await self._call(
            "delete_group_album", group_id=group_id, album_id=album_id,
        ) or {}

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
        # SnowLuma returns camelCase `mediaList`; also accept snake_case / legacy `media`
        return list(
            data.get("mediaList")
            or data.get("media_list")
            or data.get("media")
            or []
        )
