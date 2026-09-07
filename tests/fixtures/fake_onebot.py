"""Fake OneBot 适配器（契约测试用，tests/fixtures）。

- 支持注入目录树数据与故障（按 folder_id 抛错）
- 记录每次 call 时间，用于断言限速间隔（AC5）
"""

from __future__ import annotations

import time

from core.domain.enums import CapabilityState
from core.domain.resource import (
    FileSystemInfo,
    GroupFile,
    GroupFileList,
    GroupFolder,
    GroupMember,
)
from ports.onebot_api import OneBotApiPort


class FakeOneBotApi(OneBotApiPort):
    def __init__(self, tree: dict, fs_info: FileSystemInfo | None = None,
                 interval: float = 0.05, fail_folders: set[str] | None = None,
                 group_ids: list[str] | None = None, bot_qq: str = "10001",
                 members_by_group: dict[str, list] | None = None):
        """tree: {None: (files, folders), folder_id: (files, folders)}"""
        self.tree = tree
        self.fs_info = fs_info
        self.interval = interval
        self.fail_folders = fail_folders or set()
        self.group_ids = group_ids or ["g1"]
        self.bot_qq = bot_qq
        self.members_by_group = members_by_group or {}
        self.call_times: list[float] = []
        self.calls: list[str] = []
        self.members = [GroupMember(bot_qq, "Alice", "owner")]
        # v1.2 云端保存记录
        self.sent_messages: list[dict] = []      # {group_id, text, message_id}
        self.essence_set: list[str] = []         # message_id 列表（设为精华）
        self.essences: dict[str, list] = {}      # group_id -> 精华条目（回读验证）
        self.album_uploads: list[dict] = []      # {group_id, album_id, album_name, file}
        self.essence_deleted: list[str] = []     # delete_essence_msg 记录
        self.next_message_id = 90001
        self.drop_first_set = 0                   # 模拟 QQ 丢设次数（回读验证重试用）

    async def _guard(self, action: str, folder_id):
        if folder_id in self.fail_folders:
            self.calls.append(action)
            self.call_times.append(time.monotonic())
            raise RuntimeError(f"simulated failure: {action} on folder {folder_id}")
        self.calls.append(action)
        self.call_times.append(time.monotonic())

    def _list(self, folder_id):
        files, folders = self.tree.get(folder_id, ([], []))
        return GroupFileList(
            group_id="g1",
            files=[GroupFile(**f) for f in files],
            folders=[GroupFolder(**fd) for fd in folders],
            complete=True,
        )

    async def list_group_root(self, group_id: str) -> GroupFileList:
        await self._guard("get_group_root_files", None)
        return self._list(None)

    async def list_group_folder(self, group_id: str, folder_id: str = "", folder: str = "") -> GroupFileList:
        await self._guard("get_group_files_by_folder", folder_id or folder)
        return self._list(folder_id or folder or None)

    async def get_group_fs_info(self, group_id: str) -> FileSystemInfo:
        self.calls.append("get_group_file_system_info")
        self.call_times.append(time.monotonic())
        return self.fs_info or FileSystemInfo(file_count=0, limit_count=1000, used_space=0, total_space=10 * 1024 ** 3)

    async def get_group_file_url(self, group_id: str, file_id: str, busid: int | None = None, name: str = "") -> str:
        return f"https://fake/download/{file_id}"

    async def get_group_album_media_list(self, group_id: str, album_id: str) -> list:
        self.calls.append(f"get_group_album_media_list:{group_id}:{album_id}")
        return list(getattr(self, "album_media", {}).get(f"{group_id}:{album_id}", []))

    async def get_group_album_list(self, group_id: str) -> list:
        self.calls.append(f"get_group_album_list:{group_id}")
        return list(getattr(self, "albums", {}).get(group_id, []))

    async def create_group_album(self, group_id: str, album_name: str, album_desc: str = "") -> dict:
        self.calls.append(f"create_group_album:{group_id}:{album_name}")
        albums = self.albums.setdefault(group_id, [])
        new_id = f"created-{len(albums) + 1}"
        albums.append({"album_id": new_id, "name": album_name, "desc": album_desc})
        return {"album_id": new_id, "name": album_name, "desc": album_desc}

    async def delete_group_album_media(self, group_id: str, album_id: str, lloc: str) -> None:
        self.calls.append(f"del_group_album_media:{group_id}:{album_id}:{lloc}")

    async def comment_group_album_media(self, group_id: str, album_id: str, lloc: str, content: str) -> None:
        self.calls.append(f"do_group_album_comment:{group_id}:{album_id}:{lloc}")

    async def like_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        self.calls.append(f"set_group_album_media_like:{group_id}:{album_id}:{batch_id}")

    async def unlike_group_album_media(self, group_id: str, album_id: str, batch_id: str, lloc: str = "") -> None:
        self.calls.append(f"cancel_group_album_media_like:{group_id}:{album_id}:{batch_id}")

    async def get_qun_album_list(self, group_id: str) -> list:
        self.calls.append(f"get_qun_album_list:{group_id}")
        return list(getattr(self, "albums", {}).get(group_id, []))

    async def get_essence_msg_list(self, group_id: str) -> list:
        self.calls.append(f"get_essence_msg_list:{group_id}")
        return list(getattr(self, "essences", {}).get(group_id, []))

    async def get_group_info(self, group_id: str, no_cache: bool = False) -> dict:
        return {"group_id": group_id,
                 "group_name": getattr(self, "group_names", {}).get(group_id, f"群{group_id}")}

    async def get_group_honor_info(self, group_id: str, honor_type=None) -> dict:
        self.calls.append(f"get_group_honor_info:{group_id}")
        return {}

    async def get_group_system_msg(self, group_id: str = "", only_pending=False, count=50) -> dict:
        self.calls.append(f"get_group_system_msg:{group_id}")
        return {}

    async def upload_group_file(self, group_id, file_path, name="", folder_id=None, folder="", upload_file=True) -> None:
        self.calls.append(f"upload_group_file:{group_id}:{name}")

    async def delete_group_file(self, group_id, file_id, busid=None) -> None:
        self.calls.append(f"delete_group_file:{group_id}:{file_id}")

    async def rename_group_file(self, group_id, file_id, current_parent_directory, new_name) -> None:
        self.calls.append(f"rename_group_file:{group_id}:{file_id}:{current_parent_directory}:{new_name}")

    async def move_group_file(self, group_id, file_id, current_parent_directory, target_parent_directory) -> None:
        self.calls.append(f"move_group_file:{group_id}:{file_id}:{target_parent_directory}")

    async def list_group_members(self, group_id: str, no_cache: bool = False) -> list[GroupMember]:
        return self.members_by_group.get(group_id, self.members)

    async def get_group_member_info(self, group_id: str, user_id: str, no_cache: bool = False) -> dict:
        self.calls.append(f"get_group_member_info:{group_id}")
        ms = self.members_by_group.get(group_id, self.members)
        m = next((x for x in ms if x.user_id == str(user_id)), None)
        return {"user_id": int(user_id), "role": m.role if m else "member"}

    async def list_groups(self, no_cache: bool = False) -> list[dict]:
        return [
            {"group_id": gid, "group_name": f"群{gid}"}
            for gid in getattr(self, "group_ids", ["g1"])
        ]

    async def get_login_info(self) -> dict:
        return {"user_id": int(getattr(self, "bot_qq", "10001"))}

    async def set_group_name(self, group_id: str, name: str) -> None:
        self.calls.append(f"set_group_name:{group_id}:{name}")

    async def create_group_file_folder(self, group_id: str, folder_name: str, parent_id: str = "/") -> None:
        self.calls.append(f"create_group_file_folder:{group_id}:{folder_name}:{parent_id}")

    async def delete_group_file_folder(self, group_id: str, folder_id: str) -> None:
        self.calls.append(f"delete_group_file_folder:{group_id}:{folder_id}")

    async def rename_group_file_folder(self, group_id: str, folder_id: str, new_name: str) -> None:
        self.calls.append(f"rename_group_file_folder:{group_id}:{folder_id}:{new_name}")

    async def set_group_add_option(self, group_id: str, add_type: int) -> None:
        self.calls.append(f"set_group_add_option:{group_id}:{add_type}")

    async def set_group_remark(self, group_id: str, remark: str) -> None:
        self.calls.append(f"set_group_remark:{group_id}:{remark}")

    async def send_group_msg(self, group_id: str, message: list) -> dict:
        self.calls.append("send_group_msg")
        text = " ".join(
            str((s.get("data") or {}).get("text") or "")
            for s in message if isinstance(s, dict) and s.get("type") == "text"
        )
        mid = str(self.next_message_id)
        self.next_message_id += 1
        self.sent_messages.append(
            {"group_id": group_id, "text": text, "message_id": mid}
        )
        return {"message_id": mid}

    async def set_essence_msg(self, message_id: str) -> None:
        self.calls.append("set_essence_msg")
        if self.drop_first_set > 0:
            self.drop_first_set -= 1
            return  # 模拟 QQ 丢设：本次设精无效
        self.essence_set.append(str(message_id))
        m = next((x for x in self.sent_messages
                  if x["message_id"] == str(message_id)), None)
        if m:
            self.essences.setdefault(m["group_id"], []).append({
                "message_id": str(message_id),
                "content": [{"type": "text", "data": {"text": m["text"]}}],
                "sender_id": self.bot_qq,
            })

    async def delete_essence_msg(self, message_id: str) -> None:
        self.calls.append("delete_essence_msg")
        self.essence_deleted.append(str(message_id))
        for k, items in list(self.essences.items()):
            self.essences[k] = [
                e for e in items if str(e.get("message_id")) != str(message_id)
            ]

    async def upload_image_to_qun_album(
        self, group_id: str, album_id: str, album_name: str, file: str
    ) -> None:
        self.calls.append("upload_image_to_qun_album")
        self.album_uploads.append(
            {"group_id": group_id, "album_id": album_id,
             "album_name": album_name, "file": file}
        )

    async def delete_group_album(self, group_id: str, album_id: str) -> dict:
        self.calls.append(f"delete_group_album:{group_id}:{album_id}")
        return {}

    async def get_group_info_ex(self, group_id: str, no_cache: bool = False) -> dict:
        return await self.get_group_info(group_id, no_cache)

    async def get_group_detail_info(self, group_id: str, no_cache: bool = False) -> dict:
        return await self.get_group_info(group_id, no_cache)

    async def download_file(self, url: str = "", base64: str = "", name: str = "") -> dict:
        self.calls.append("download_file")
        return {"file": f"/tmp/{name or 'downloaded'}"}

    async def get_file(self, file_id: str = "", file: str = "") -> dict:
        self.calls.append("get_file")
        return {"file": file or file_id, "url": f"https://fake/{file or file_id}"}

    async def get_image(self, file: str = "", file_id: str = "") -> dict:
        self.calls.append("get_image")
        return {"file": file or file_id, "url": f"https://fake/{file or file_id}"}

    async def trans_group_file(self, group_id: str, file_id: str, target_group_id: str) -> dict:
        self.calls.append(f"trans_group_file:{group_id}:{file_id}:{target_group_id}")
        return {}

    async def persist_group_file(self, group_id: str, file_id: str) -> dict:
        self.calls.append(f"persist_group_file:{group_id}:{file_id}")
        return {}

    def capability(self, action: str) -> CapabilityState:
        return CapabilityState.SUPPORTED

    async def close(self) -> None:
        pass


def build_tree(file_total: int, folder_total: int, files_per_folder: int = 20):
    """构造目录树：根目录 empty，folder_i 含 files_per_folder 个文件。"""
    tree = {None: ([], [])}
    seq = 0
    for i in range(folder_total):
        fid = f"folder_{i}"
        files = []
        for j in range(files_per_folder):
            seq += 1
            files.append(
                dict(
                    file_id=f"file_{seq}",
                    name=f"doc_{seq}.pdf",
                    size=1024 * seq,
                    busid=102,
                    uploader_id="10001",
                    uploader_name="Alice",
                    upload_time=1700000000 + seq,
                )
            )
        tree[fid] = (files, [])
        tree[None][1].append(dict(folder_id=fid, name=f"目录{i}"))
    return tree
