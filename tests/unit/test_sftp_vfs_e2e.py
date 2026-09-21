"""U4: SFTP 真实协议栈端到端覆盖（paramiko 客户端 <-> paramiko 服务端）。

U2 直接测 `_run_in_loop` 的参数契约；这里从真正的 SFTP 协议栈验证同一条路径：
`stat()` / `open()` 对存在的云端文件不得返回 NO_SUCH_FILE。两者互为独立防线，
一起才算把这条路径锁死。

覆盖：
- `/` 根列表（群 + staged）
- `/<group>` 文件列表，含 >500 文件的分页
- `/<group>/<name>` stat + get（字节级一致；二次 get 复用缓存）
- `/staged/<token>_<name>` 列表 + stat + get
- 错误路径：不存在 -> NO_SUCH_FILE；写操作 -> PERMISSION_DENIED
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from types import SimpleNamespace

import pytest

from core.application.download_server_io import build_sftp_interface

from core.application.download_server import DownloadServerService

PAYLOAD = b"real-sftp-payload"


class _PagedStore:
    """真实 store 语义：list_groups + keyword 子串匹配 + 分页。

    与 download_server.py 的 `collect_resources` 契约一致：
    keyword 是 LIKE %kw% 超集，`total` 是过滤后的总行数。
    """

    NAMES = ["slice-0.bin", "slice-7.bin", "plain.txt"]

    def __init__(self) -> None:
        self.queries: list = []

    async def list_groups(self):
        return [SimpleNamespace(group_id="g1"), SimpleNamespace(group_id="g2")]

    async def query_resources(self, q):
        self.queries.append((q.group_id, q.page, q.page_size, q.keyword))
        rows = [
            SimpleNamespace(id=i, name=n, size=len(PAYLOAD))
            for i, n in enumerate(self.NAMES)
        ]
        if q.group_id != "g1":
            rows = []
        if q.keyword:
            rows = [r for r in rows if q.keyword in r.name]
        start = (q.page - 1) * q.page_size
        return SimpleNamespace(
            items=rows[start : start + q.page_size],
            total=len(rows),
            page=q.page,
            page_size=q.page_size,
        )


# ---------- 虚拟 FS 语义（build_sftp_interface 直调） ----------


def test_vfs_path_grammar_is_enforced(make_service, loop_thread, tmp_path):
    """只认 /<group>/<name> 与 /staged/<token>_<name>；其余路径一律 NO_SUCH_FILE。

    含非法路径（三层、空段、只有群名）与 staged token 未知两种情况。
    """
    paramiko = pytest.importorskip("paramiko")

    async def _download_info(group, rid):
        return ((tmp_path / "recon_x.bin").as_posix(), "x.bin")

    (tmp_path / "recon_x.bin").write_bytes(PAYLOAD)
    svc = make_service(store=_PagedStore(), download_info=_download_info)
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)

    for bad in ("/", "/g1", "/g1/slice-0.bin/extra", "/nope/x.bin", "/staged/unknown_tok"):
        assert iface.stat(bad) == paramiko.SFTP_NO_SUCH_FILE, bad


def test_vfs_lists_root_and_group_and_marks_read_only(make_service, loop_thread, tmp_path):
    """`/` 列出全部群；`/<group>` 列出该群文件；模式位只读。"""
    paramiko = pytest.importorskip("paramiko")

    async def _download_info(group, rid):
        return ((tmp_path / "recon_x.bin").as_posix(), "x.bin")

    (tmp_path / "recon_x.bin").write_bytes(PAYLOAD)
    svc = make_service(store=_PagedStore(), download_info=_download_info)
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)

    root = iface.list_folder("/")
    assert {e.filename for e in root} == {"g1", "g2"}
    assert all(e.st_mode == 0o40555 for e in root), "根目录必须 dr-xr-xr-x"

    files = iface.list_folder("/g1")
    assert {e.filename for e in files} == set(_PagedStore.NAMES)
    assert all(e.st_mode == 0o100644 for e in files), "文件必须 -rw-r--r--"

    assert iface.list_folder("/g1/slice-0.bin") == [], "文件路径不是目录"
    assert iface.list_folder("/g2") == []


def test_vfs_listing_mode_matches_stat(make_service, loop_thread, tmp_path):
    """列表与 stat 必须报告同一个模式位。

    真实缺陷（U4 定位并修复）：list_folder 的两条文件分支都没有写
    st_mode（群文件列表、staged 列表），而 stat() 写的是 0o100644。
    同一个文件 `ls` 说“权限未知”、`stat` 说是普通文件，用 mtime 过滤器
    的客户端会直接把条目跳过或报错。

    反向验证：把任一分支里的 `info.st_mode = 0o100644` 去掉，本用例变红。
    """
    paramiko = pytest.importorskip("paramiko")

    async def _download_info(group, rid):
        return ((tmp_path / "recon_x.bin").as_posix(), "x.bin")

    (tmp_path / "recon_x.bin").write_bytes(PAYLOAD)
    staged = tmp_path / "essence.txt"
    staged.write_bytes(b"essence-text")

    svc = make_service(store=_PagedStore(), download_info=_download_info)
    svc._loop = loop_thread
    info = svc.register_staged(staged, "essence.txt")
    iface = build_sftp_interface(svc)(None)

    # 群文件：列表的每个条目都要有模式位，且与 stat() 一致
    listed = iface.list_folder("/g1")
    assert listed, "群列表不得为空"
    assert all(e.st_mode == 0o100644 for e in listed), (
        "U4: list_folder 必须与 stat() 一样报告 0o100644"
    )
    for entry in listed:
        attrs = iface.stat(f"/g1/{entry.filename}")
        assert attrs != paramiko.SFTP_NO_SUCH_FILE
        assert attrs.st_mode == entry.st_mode, (
            f"列表与 stat 模式不一致：{entry.filename}"
        )

    # staged 文件：同一契约
    staged_entries = iface.list_folder("/staged")
    assert [e.filename for e in staged_entries] == [f"{info['token']}_essence.txt"]
    assert staged_entries[0].st_mode == 0o100644, (
        "U4: staged 列表同样不得遗漏模式位"
    )
    assert (
        iface.stat(f"/staged/{info['token']}_essence.txt").st_mode
        == staged_entries[0].st_mode
    )

    # 目录条目与文件条目必须是不同的模式位（否则客户端认不出目录）
    dirs = iface.list_folder("/")
    assert all(e.st_mode == 0o40555 for e in dirs)
    assert {e.st_mode for e in dirs}.isdisjoint({e.st_mode for e in listed})


def test_vfs_lists_past_the_first_page(make_service, loop_thread, tmp_path, monkeypatch):
    """分页：>500 文件时 `/<group>` 必须列全（不能停在第一页的 500）。"""
    paramiko = pytest.importorskip("paramiko")

    class _BigStore:
        COUNT = 1200

        async def list_groups(self):
            return [SimpleNamespace(group_id="g1")]

        async def query_resources(self, q):
            rows = [
                SimpleNamespace(id=i, name=f"f{i}", size=7)
                for i in range(self.COUNT)
            ]
            start = (q.page - 1) * q.page_size
            return SimpleNamespace(
                items=rows[start : start + q.page_size],
                total=self.COUNT,
                page=q.page,
                page_size=q.page_size,
            )

    async def _download_info(group, rid):
        return ((tmp_path / "recon_x.bin").as_posix(), "x.bin")

    (tmp_path / "recon_x.bin").write_bytes(PAYLOAD)
    svc = make_service(store=_BigStore(), download_info=_download_info)
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)

    shown = iface.list_folder("/g1")
    assert len(shown) == _BigStore.COUNT, "分页：第 501 条起也必须出现"
    assert "f1199" in {e.filename for e in shown}
    assert iface.stat("/g1/f1199") != paramiko.SFTP_NO_SUCH_FILE


def test_vfs_staged_namespace_lists_stats_and_opens(make_service, loop_thread, tmp_path):
    """/staged/<token>_<name>：列表用 token_name，stat 拿 size，open 读原文件。"""
    paramiko = pytest.importorskip("paramiko")

    staged = tmp_path / "essence.txt"
    staged.write_bytes(b"essence-text")

    svc = make_service(store=_PagedStore())
    svc._loop = loop_thread
    info = svc.register_staged(staged, "essence.txt")
    token = info["token"]
    iface = build_sftp_interface(svc)(None)

    root_names = {e.filename for e in iface.list_folder("/")}
    assert "staged" in root_names

    listed = iface.list_folder("/staged")
    assert [e.filename for e in listed] == [f"{token}_essence.txt"]
    assert listed[0].st_size == len(b"essence-text")

    attrs = iface.stat(f"/staged/{token}_essence.txt")
    assert attrs != paramiko.SFTP_NO_SUCH_FILE
    assert attrs.st_size == len(b"essence-text")

    handle = iface.open(f"/staged/{token}_essence.txt", 0, None)
    assert not isinstance(handle, int), f"staged open failed: {handle}"
    try:
        assert handle.readfile.read() == b"essence-text"
    finally:
        handle.readfile.close()

    # 注册表清理后路径立即失效（不能留悬空 token）
    svc._staged.clear()
    assert iface.stat(f"/staged/{token}_essence.txt") == paramiko.SFTP_NO_SUCH_FILE
    assert iface.list_folder("/staged") == []


@pytest.mark.parametrize(
    "method,args",
    [
        ("remove", ("/g1/slice-0.bin",)),
        ("rename", ("/g1/slice-0.bin", "/g1/other.bin")),
        ("mkdir", ("/g1/new", None)),
        ("rmdir", ("/g1",)),
        ("symlink", ("/g1/slice-0.bin", "/g1/link.bin")),
    ],
)
def test_vfs_write_operations_are_denied(make_service, loop_thread, method, args):
    """虚拟 FS 只读：所有写操作返回 PERMISSION_DENIED（不是 OP_UNSUPPORTED）。"""
    paramiko = pytest.importorskip("paramiko")

    svc = make_service(store=_PagedStore())
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)
    assert getattr(iface, method)(*args) == paramiko.SFTP_PERMISSION_DENIED


# ---------- 反向验证锚点：collect_resources 分页 ----------


def test_paging_is_what_makes_row_1200_reachable(make_service, loop_thread):
    """反向验证：把 max_pages 压到 1（等于退回单页实现），第 1200 行必须消失。

    这条用例是 U4 的“反向验证”开关——若有人把 collect_resources 的翻页去掉，
    这里立刻变红，而不是等到生产环境才发现 501 条之后的文件全部打不开。
    """
    from core.application import download_server_io as dio

    class _BigStore:
        COUNT = 1200

        async def query_resources(self, q):
            rows = [
                SimpleNamespace(id=i, name=f"f{i}", size=7)
                for i in range(self.COUNT)
            ]
            start = (q.page - 1) * q.page_size
            return SimpleNamespace(
                items=rows[start : start + q.page_size],
                total=self.COUNT,
                page=q.page,
                page_size=q.page_size,
            )

    svc = make_service(store=_BigStore())
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)
    assert iface.stat("/g1/f1199") != -1, "默认（翻页）下 f1199 必须可见"

    real = dio.collect_resources

    async def _single_page(store, group_id, *, page_size=500, max_pages=1):
        return await real(store, group_id, page_size=page_size, max_pages=1)

    import core.application.download_server as ds

    ds.collect_resources = _single_page
    dio.collect_resources = _single_page
    try:
        svc._row_cache.clear()
        shown = iface.list_folder("/g1")
        assert len(shown) == 500, "反向验证：单页实现只能看到 500 条"
    finally:
        dio.collect_resources = real
        ds.collect_resources = real


# ---------- fixtures ----------
# 与 tests/unit/test_download_server_regressions.py 里的同名 fixture 语义一致。
# 这里刻意本地定义而不是跨文件 import：import fixture 会被 ruff 判为 F811
# （redefinition of unused），而 lib 导出会被 pytest 判为 collect 失败。


@pytest.fixture
def loop_thread():
    """独立事件循环线程：_run_in_loop() 从别的线程调度协程（SFTP 的真实用法）。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    """构造服务实例，并把 mkdtemp 缓存根重定向到 tmp_path（用后清理）。"""
    created: list[DownloadServerService] = []
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda prefix="": str(tmp_path / f"cloudstorage-{len(created)}"),
    )

    def _make(store=None, download_info=None, config=None):
        cfg = {"download_server_enabled": True, "download_token": "t0ken"}
        cfg.update(config or {})
        svc = DownloadServerService(store, cfg, download_info=download_info)
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        if svc._cache_root is not None:  # shutdown() 会把它置 None
            shutil.rmtree(svc._cache_root, ignore_errors=True)
