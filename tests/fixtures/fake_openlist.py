"""Fake OpenList 控制面（契约测试用，tests/fixtures）。

- 不联网：目录树/离线下载任务全部内存模拟
- 记录 submitted/renames 供行为断言（去重幂等、D1 改名）
"""

from __future__ import annotations

from adapters.external.openlist import DirectLink, NetFile, OfflineTask


class FakeOpenListClient:
    """OpenListClient 假实现（控制面：任务/目录/改名/直链）。"""

    def __init__(self):
        self.capability = "OK"
        self.dirs: set[str] = set()
        self.files: dict[str, list[NetFile]] = {}  # dir -> entries
        self.undone: dict[str, OfflineTask] = {}
        self.done_tasks: dict[str, OfflineTask] = {}
        self.submitted: list[tuple[list[str], str]] = []
        self.renames: list[tuple[str, str]] = []  # (old_path, new_name)
        self._task_seq = 0

    async def stat(self, path: str) -> NetFile | None:
        parent, _, name = path.rstrip("/").rpartition("/")
        for f in self.files.get(parent or "/", []):
            if f.name == name and not f.is_dir:
                return f
        return None

    async def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    async def submit_offline_download(
        self, urls: list[str], remote_dir: str
    ) -> list[OfflineTask]:
        self._task_seq += 1
        tid = f"oltask_{self._task_seq}"
        task = OfflineTask(
            id=tid,
            name=urls[0].rsplit("/", 1)[-1] if urls else "",
            state="pending",
            status="running",
            progress=0.0,
            error="",
        )
        self.undone[tid] = task
        self.submitted.append((list(urls), remote_dir))
        return [task]

    async def tasks_undone(self) -> list[OfflineTask]:
        return list(self.undone.values())

    async def tasks_done(self) -> list[OfflineTask]:
        return list(self.done_tasks.values())

    async def task_cancel(self, task_id: str) -> bool:
        return self.undone.pop(task_id, None) is not None

    async def task_retry(self, task_id: str) -> bool:
        return task_id in self.done_tasks

    async def get_raw_url(self, path: str) -> DirectLink:
        return DirectLink(url=f"http://openlist.test/dl{path}")

    async def list_dir(self, path: str) -> list[NetFile]:
        return list(self.files.get(path, []))

    async def list_dir_page(
        self, path: str, page: int, per_page: int = 200
    ) -> tuple[list[NetFile], bool]:
        entries = list(self.files.get(path, []))
        start = (page - 1) * per_page
        chunk = entries[start:start + per_page]
        return chunk, (start + len(chunk)) < len(entries)

    async def rename(self, path: str, new_name: str) -> None:
        parent, _, old = path.rstrip("/").rpartition("/")
        files = self.files.get(parent or "/", [])
        for i, f in enumerate(files):
            if f.name == old:
                files[i] = NetFile(
                    name=new_name,
                    size=f.size,
                    is_dir=f.is_dir,
                    modified=f.modified,
                )
                self.renames.append((path, new_name))
                return
        raise KeyError(path)


class FakeDownloadServer:
    """本机下载服务假实现（REQ-16 守卫用）。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.http_port = 6186 if enabled else 0

    def download_url(self, group_id: str, id: int) -> str:
        return f"http://127.0.0.1:{self.http_port}/dl/{group_id}/{id}"
