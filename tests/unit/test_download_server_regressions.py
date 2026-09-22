"""DownloadServerService 回归测试。

- M7: SFTP open() 必须复用缓存（不能每次都重新取 CDN 链接并整份下载）
- M8: 分卷重组出的 recon_* 文件在响应发送/拷贝完成后必须回收
- M20: SFTP accept 循环每连接一个线程，慢客户端不得阻塞后续连接
- 低危: HTTP 端点支持 Range/206/416；SFTP 目录与查找必须翻页取全
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
from types import SimpleNamespace

import pytest

from core.application import download_server_io as dio
from core.application.download_server import DownloadServerService
from core.application.download_server_io import (
    build_sftp_interface,
    cache_path,
    collect_resources,
    join_sftp_connections,
    parse_range,
    serve_local_file,
    start_sftp_server,
)


class _Writer:
    """asyncio.StreamWriter 替身：只记录写出的字节。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStore:
    """按页返回资源行，模拟 SqliteMetaStore 的分页语义。"""

    def __init__(self, count: int) -> None:
        self.count = count
        self.pages: list[int] = []

    async def query_resources(self, q):
        self.pages.append(q.page)
        start = (q.page - 1) * q.page_size
        items = [
            SimpleNamespace(id=i, name=f"f{i}", size=10 + i)
            for i in range(start, min(start + q.page_size, self.count))
        ]
        return SimpleNamespace(
            items=items, total=self.count, page=q.page, page_size=q.page_size
        )


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


# ---------- 低危: Range / 206 / 416 ----------


@pytest.mark.parametrize(
    "header,total,expected",
    [
        (None, 10, None),
        ("", 10, None),
        ("bytes=0-3", 10, (0, 3)),
        ("bytes=4-", 10, (4, 9)),
        ("bytes=-4", 10, (6, 9)),
        ("bytes=0-99", 10, (0, 9)),
        ("bytes=0-1,5-6", 10, None),  # 多段：退回整份 200
        ("items=0-1", 10, None),
        ("bytes=10-", 10, "unsatisfiable"),
        ("bytes=5-2", 10, "unsatisfiable"),
    ],
)
def test_parse_range(header, total, expected):
    got = parse_range(header, total)
    if expected == "unsatisfiable":
        assert isinstance(got, str)
    else:
        assert got == expected


async def test_serve_local_file_answers_range_and_reclaims_recon(tmp_path):
    """低危 + M8: 带 Range 的请求回 206，且响应完成后删除 recon_* 源文件。"""
    src = tmp_path / "recon_deadbeef_clip.mp4"
    src.write_bytes(b"0123456789")
    w = _Writer()
    await serve_local_file(w, src, "clip.mp4", "bytes=2-5")
    head, _, body = bytes(w.buf).partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 206 Partial Content")
    assert b"Content-Range: bytes 2-5/10" in head
    assert b"Accept-Ranges: bytes" in head
    assert body == b"2345"
    assert not src.exists(), "M8: recon_* 重组文件必须在响应完成后删除"


async def test_serve_local_file_full_body_advertises_ranges(tmp_path):
    src = tmp_path / "recon_1_plain.bin"
    src.write_bytes(b"abcdef")
    w = _Writer()
    await serve_local_file(w, src, "plain.bin", None)
    assert bytes(w.buf).startswith(b"HTTP/1.1 200 OK")
    assert b"Accept-Ranges: bytes" in bytes(w.buf)
    assert bytes(w.buf).endswith(b"abcdef")


async def test_serve_local_file_keeps_non_recon_files(tmp_path):
    """清理必须只针对重组产物，普通缓存文件不得被误删。"""
    src = tmp_path / "cached.bin"
    src.write_bytes(b"abcdef")
    w = _Writer()
    await serve_local_file(w, src, "cached.bin", None)
    assert src.exists()


async def test_serve_local_file_unsatisfiable_range_is_416(tmp_path):
    src = tmp_path / "recon_2_small.bin"
    src.write_bytes(b"abc")
    w = _Writer()
    await serve_local_file(w, src, "small.bin", "bytes=99-")
    assert bytes(w.buf).startswith(b"HTTP/1.1 416")
    assert b"Content-Range: bytes */3" in bytes(w.buf)


# ---------- M8: ensure_local 拷贝完成后回收 ----------


async def test_ensure_local_reclaims_recon_after_copy(make_service, tmp_path):
    src = tmp_path / "recon_cccc_vol.zip"
    src.write_bytes(b"vol-data")

    async def _download_info(group, rid):
        return (src.as_posix(), "vol.zip")

    svc = make_service(download_info=_download_info)
    target = await svc.ensure_local("g1", 1, "vol.zip")
    assert target is not None and target.read_bytes() == b"vol-data"
    assert not src.exists(), "M8: 拷贝进 SMB 目录后必须回收 recon_* 源文件"


# ---------- 低危: 翻页 ----------


async def test_collect_resources_pages_past_the_first_page():
    store = _FakeStore(1200)
    items = await collect_resources(store, "g1")
    assert len(items) == 1200
    assert store.pages == [1, 2, 3]
    assert items[500].name == "f500"


def test_find_row_reaches_rows_past_the_first_page(make_service, loop_thread):
    """低危: 固定 page_size=500 且不翻页时，第 501 条之后既列不出也打不开。"""
    svc = make_service(store=_FakeStore(1200))
    svc._loop = loop_thread
    assert svc._find_row("g1", "f1100")["id"] == 1100


def test_sftp_listing_contains_rows_past_the_first_page(make_service, loop_thread):
    svc = make_service(store=_FakeStore(1200))
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)
    names = {e.filename for e in iface.list_folder("/g1")}
    assert len(names) == 1200
    assert "f1199" in names


# ---------- M7: SFTP open() 缓存复用 ----------


async def test_sftp_open_reuses_cache_on_second_open(make_service, loop_thread, tmp_path):
    """M7: 第二次 open 不得再解析 CDN 链接/重新下载。"""
    src = tmp_path / "recon_aaaa_movie.mp4"
    src.write_bytes(b"payload-bytes")
    calls: list[tuple] = []

    async def _download_info(group, rid):
        calls.append((group, rid))
        return (src.as_posix(), "movie.mp4")

    svc = make_service(store=_FakeStore(1), download_info=_download_info)
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)

    h1 = iface.open("/g1/f0", 0, None)
    assert not isinstance(h1, int), f"sftp open failed: {h1}"
    assert h1.readfile.read() == b"payload-bytes"
    h1.readfile.close()

    h2 = iface.open("/g1/f0", 0, None)
    assert not isinstance(h2, int), f"sftp open failed: {h2}"
    assert h2.readfile.read() == b"payload-bytes"
    h2.readfile.close()

    assert calls == [("g1", 0)], "M7: 缓存命中时不得再次调用 download_info"
    assert not src.exists(), "M7: 已缓存的重组源文件应当被回收"


# ---------- M20: 每连接一线程 ----------


def _install_fake_paramiko(monkeypatch, transport_cls):
    """注入假 paramiko：只关心 accept 循环是否并发起线程。"""
    fake = types.ModuleType("paramiko")
    sftp_server = types.ModuleType("paramiko.sftp_server")

    class _Attrs:
        def __init__(self):
            self.filename = None
            self.st_mode = None
            self.st_size = None

    class _Handle:
        def __init__(self, *a, **kw):
            self.readfile = None

    class _ServerInterface:
        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            return 0

    class _SFTPServerInterface:
        def __init__(self, server=None, *a, **kw):
            self.server = server

    class _RSAKey:
        @staticmethod
        def generate(bits):
            return "fake-key"

    class _SFTPServer:
        def __init__(self, *a, **kw):
            pass

    fake.RSAKey = _RSAKey
    fake.ServerInterface = _ServerInterface
    fake.SFTPServerInterface = _SFTPServerInterface
    fake.SFTPHandle = _Handle
    fake.SFTPAttributes = _Attrs
    fake.Transport = transport_cls
    fake.AUTH_SUCCESSFUL = 0
    fake.AUTH_FAILED = 1
    fake.SFTP_NO_SUCH_FILE = 2
    fake.SFTP_PERMISSION_DENIED = 3
    fake.SFTP_FAILURE = 4
    fake.SFTP_OP_UNSUPPORTED = 8
    sftp_server.SFTPServer = _SFTPServer
    fake.sftp_server = sftp_server
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    monkeypatch.setitem(sys.modules, "paramiko.sftp_server", sftp_server)


def _wait_for_sftp_socket(svc, timeout: float = 10.0) -> int:
    """等 accept 循环发布监听 socket，返回内核分配的端口。"""
    deadline = time.time() + timeout
    while svc._sftp_sock is None and time.time() < deadline:
        time.sleep(0.01)
    assert svc._sftp_sock is not None, "sftp accept 循环未启动"
    return svc._sftp_sock.getsockname()[1]


def _stop_sftp(svc) -> None:
    """关掉监听 socket 并回收会话线程（shutdown() 的非 async 版）。

    shutdown(SHUT_RDWR) 是必需的：只 close() 不会叫醒卡在 accept() 里的线程，
    端口会一直占着。
    """
    sock = svc._sftp_sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        svc._sftp_sock = None
    if svc._sftp_thread is not None and svc._sftp_thread.is_alive():
        svc._sftp_thread.join(timeout=5)
    join_sftp_connections(svc)


def test_sftp_accept_loop_serves_connections_concurrently(make_service, monkeypatch):
    """M20: 第一条会话未结束时，第二条连接也必须被 accept（每连接一线程）。"""
    handshakes = itertools.count()
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    class _SlowTransport:
        def __init__(self, sock):
            self.sock = sock
            self._active = True

        def add_server_key(self, key):
            pass

        def set_subsystem_handler(self, *a, **kw):
            pass

        def start_server(self, server=None):
            with lock:
                if next(handshakes) >= 1:
                    both_started.set()
            release.wait(timeout=10.0)  # 模拟慢会话（串行实现里会阻塞 accept）
            self._active = False

        def is_active(self):
            return self._active

        def close(self):
            self._active = False

    _install_fake_paramiko(monkeypatch, _SlowTransport)
    svc = make_service()
    svc.host = "127.0.0.1"
    svc.sftp_port = 0  # 由内核分配端口
    start_sftp_server(svc)
    port = _wait_for_sftp_socket(svc)

    clients = []
    try:
        clients.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        clients.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        # 事件驱动等待：不再用 sleep 轮询 + 3s deadline（flaky）
        assert both_started.wait(timeout=10.0), (
            "M20: 第二条连接在第一条会话结束前未被处理（accept 循环仍是串行的）"
        )
    finally:
        release.set()
        for c in clients:
            c.close()
        _stop_sftp(svc)
    assert svc._sftp_conn_threads == [], "T2: join_sftp_connections 后不得残留连接线程"


# ---------- T2: 真实 paramiko 冒烟（不注入 fake） ----------


PAYLOAD = b"real-sftp-payload"


class _SftpStore:
    """SFTP 会话用到的 store 面：list_groups + 分页查询（keyword 子串匹配）。"""

    def __init__(self, files: dict) -> None:
        self.files = files
        self.queries: list = []

    async def list_groups(self):
        return [SimpleNamespace(group_id="g1")]

    async def query_resources(self, q):
        self.queries.append((q.group_id, q.page, q.page_size, q.keyword))
        rows = [
            SimpleNamespace(id=i, name=n, size=s)
            for i, (n, s) in enumerate(self.files.items())
        ]
        if q.keyword:
            rows = [r for r in rows if q.keyword in r.name]
        start = (q.page - 1) * q.page_size
        return SimpleNamespace(
            items=rows[start : start + q.page_size],
            total=len(rows),
            page=q.page,
            page_size=q.page_size,
        )


@pytest.fixture
def real_sftp(make_service, loop_thread, tmp_path):
    """真实 paramiko 的 SFTP 服务（不注入 fake；本机已装 paramiko）。"""
    paramiko = pytest.importorskip("paramiko")

    src = tmp_path / "recon_5f00_real.bin"
    src.write_bytes(PAYLOAD)

    async def _download_info(group, rid):
        return (src.as_posix(), "real.bin")

    svc = make_service(
        store=_SftpStore({"real.bin": len(PAYLOAD)}), download_info=_download_info
    )
    svc._loop = loop_thread
    svc.host = "127.0.0.1"
    svc.sftp_port = 0
    start_sftp_server(svc)
    port = _wait_for_sftp_socket(svc)
    try:
        yield svc, port, paramiko
    finally:
        _stop_sftp(svc)


def _sftp_login(paramiko, port, password, username="cloud"):
    transport = paramiko.Transport(("127.0.0.1", port))
    try:
        transport.connect(username=username, password=password)
    except Exception:
        transport.close()
        raise
    return transport


def test_real_sftp_session_lists_stats_and_reads(real_sftp):
    """T2/H6: 真实 paramiko 会话——认证、open_sftp()、listdir/stat/open 全链路。"""
    svc, port, paramiko = real_sftp
    transport = _sftp_login(paramiko, port, svc.token)
    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            assert "g1" in sftp.listdir("/")
            assert sftp.stat("/g1/real.bin").st_size == len(PAYLOAD)
            with sftp.open("/g1/real.bin", "rb") as fh:
                assert fh.read() == PAYLOAD
            with pytest.raises(IOError):
                sftp.stat("/g1/missing.bin")
        finally:
            sftp.close()
    finally:
        transport.close()


@pytest.mark.parametrize("password", ["", "wrong-token"])
def test_real_sftp_session_rejects_bad_credentials(real_sftp, password):
    """T2: 空口令/错误口令必须被拒（认证面真的有覆盖，而不是只数线程）。"""
    _svc, port, paramiko = real_sftp
    transport = paramiko.Transport(("127.0.0.1", port))
    try:
        with pytest.raises(paramiko.AuthenticationException):
            transport.connect(username="cloud", password=password)
    finally:
        transport.close()


def test_real_sftp_connection_threads_are_reaped(real_sftp):
    """T2: 会话结束后连接线程必须被 join_sftp_connections() 回收。"""
    svc, port, paramiko = real_sftp
    transport = _sftp_login(paramiko, port, svc.token)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        sftp.listdir("/")
        assert len(svc._sftp_conn_threads) == 1
    finally:
        sftp.close()
        transport.close()
    join_sftp_connections(svc)
    assert svc._sftp_conn_threads == [], "T2: join 之后不得残留会话线程"


# ---------- L6: 句柄/socket 泄漏与 drain 超时 ----------


async def test_shutdown_releases_the_sftp_port(real_sftp):
    """L6: shutdown() 必须叫醒 accept 循环；否则端口一直占着（重载后 bind 失败）。"""
    svc, port, _paramiko = real_sftp
    await svc.shutdown()
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


class _StuckWriter(_Writer):
    """客户端不读（TCP 零窗口）：drain 永远不返回。"""

    async def drain(self) -> None:
        await asyncio.sleep(3600)


async def test_serve_local_file_times_out_on_a_stalled_client(tmp_path, monkeypatch):
    """L6: drain 必须有超时；超时后 recon_* 源文件仍要被回收。"""
    monkeypatch.setattr(dio, "_DRAIN_TIMEOUT", 0.05)
    src = tmp_path / "recon_stall.bin"
    src.write_bytes(b"x" * (1 << 17))
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            serve_local_file(_StuckWriter(), src, "stall.bin", None), timeout=5.0
        )
    assert time.monotonic() - started < 2.0, "L6: drain 必须在 _DRAIN_TIMEOUT 内超时"
    assert not src.exists(), "L6: 超时后 recon_* 源文件必须被回收"


def test_failed_transport_handshake_closes_the_accepted_socket(make_service, monkeypatch):
    """L6: Transport() 构造失败时必须关掉已 accept 的 socket（否则泄 fd）。"""

    class _BoomTransport:
        def __init__(self, sock):
            raise RuntimeError("transport ctor failed")

    _install_fake_paramiko(monkeypatch, _BoomTransport)
    svc = make_service()
    svc.host = "127.0.0.1"
    svc.sftp_port = 0
    start_sftp_server(svc)
    port = _wait_for_sftp_socket(svc)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        data = None
        deadline = time.time() + 5.0
        while time.time() < deadline:
            client.settimeout(0.5)
            try:
                data = client.recv(1)
                break
            except TimeoutError:
                continue
        assert data == b"", "L6: accept 到的 socket 未被关闭（fd 泄漏）"
    finally:
        client.close()
        _stop_sftp(svc)


def test_sftp_resolve_turns_store_failures_into_no_such_file(make_service, loop_thread):
    """L6: store/loop 异常必须变成 SFTP_NO_SUCH_FILE，而不是打断整个 SSH 会话。"""
    paramiko = pytest.importorskip("paramiko")

    class _BrokenStore:
        async def query_resources(self, q):
            raise RuntimeError("store down")

    svc = make_service(store=_BrokenStore())
    svc._loop = loop_thread
    iface = build_sftp_interface(svc)(None)
    assert iface.stat("/g1/f0") == paramiko.SFTP_NO_SUCH_FILE
    assert iface.lstat("/g1/f0") == paramiko.SFTP_NO_SUCH_FILE


def test_sftp_open_closes_the_file_when_the_handle_cannot_be_built(
    make_service, loop_thread, monkeypatch, tmp_path
):
    """L6: SFTPHandle 构造失败时，已打开的 fh 必须被关闭。"""
    paramiko = pytest.importorskip("paramiko")

    src = tmp_path / "recon_fd.bin"
    src.write_bytes(PAYLOAD)

    async def _download_info(group, rid):
        return (src.as_posix(), "fd.bin")

    svc = make_service(store=_FakeStore(1), download_info=_download_info)
    svc._loop = loop_thread

    opened: list = []

    class _Tracked:
        def __init__(self, fh):
            self._fh = fh
            self.closed = False

        def close(self):
            self.closed = True
            self._fh.close()

        def __getattr__(self, item):
            return getattr(self._fh, item)

    class _TrackedPath:
        def open(self, mode):
            fh = _Tracked(src.open(mode))
            opened.append(fh)
            return fh

    monkeypatch.setattr(dio, "materialize_to_cache", lambda svc_, info: _TrackedPath())

    class _BoomHandle:
        def __init__(self, *a, **kw):
            raise RuntimeError("no handle")

    monkeypatch.setattr(paramiko, "SFTPHandle", _BoomHandle)
    iface = build_sftp_interface(svc)(None)
    assert iface.open("/g1/f0", 0, None) == paramiko.SFTP_FAILURE
    assert opened, "open() 未走到 SFTPHandle 构造"
    assert all(fh.closed for fh in opened), "L6: 失败路径必须关闭已打开的 fh"


def test_materialize_cleans_recon_when_the_copy_fails(
    make_service, loop_thread, monkeypatch, tmp_path
):
    """L6: 拷贝失败也必须回收 recon_* 源文件。"""
    paramiko = pytest.importorskip("paramiko")

    src = tmp_path / "recon_fail.bin"
    src.write_bytes(PAYLOAD)

    async def _download_info(group, rid):
        return (src.as_posix(), "fail.bin")

    svc = make_service(store=_FakeStore(1), download_info=_download_info)
    svc._loop = loop_thread

    def _boom(sp, cache):
        raise OSError("disk full")

    monkeypatch.setattr(dio, "_copy_into_cache", _boom)
    iface = build_sftp_interface(svc)(None)
    assert iface.open("/g1/f0", 0, None) == paramiko.SFTP_FAILURE
    assert not src.exists(), "L6: 拷贝失败后 recon_* 源文件不得泄漏"


def test_sftp_open_rematerializes_a_truncated_cache(make_service, loop_thread, tmp_path):
    """L6: cache.exists() 不算命中——空/截断的缓存必须重新物化。"""
    src = tmp_path / "recon_full.bin"
    src.write_bytes(PAYLOAD)
    calls: list = []

    async def _download_info(group, rid):
        calls.append((group, rid))
        return (src.as_posix(), "full.bin")

    svc = make_service(store=_FakeStore(1), download_info=_download_info)
    svc._loop = loop_thread
    stale = cache_path(svc, {"group": "g1", "id": 0, "name": "f0"})
    stale.write_bytes(b"")  # 上一次写入被截断的占位
    iface = build_sftp_interface(svc)(None)
    handle = iface.open("/g1/f0", 0, None)
    assert not isinstance(handle, int), f"sftp open failed: {handle}"
    assert handle.readfile.read() == PAYLOAD
    handle.readfile.close()
    assert calls == [("g1", 0)], "L6: 空缓存必须被重新物化，不能当命中"


def test_cache_path_never_escapes_the_cache_dir(make_service):
    """缓存文件名取末段路径分量：含 / 或 .. 的资源名不得把缓存写到 _cache_dir 之外。

    修复前 cache_path 直接拼接 info["name"]，`Path.__truediv__` 会把其中的
    分隔符当路径处理 ⇒ 缓存文件落在 _cache_dir 之外（可写任意路径）。
    """
    svc = make_service(store=_FakeStore(1))
    hostile = ["../../etc/passwd", "sub/dir/f0.bin", "..", ".", "", "a/../../b"]
    for name in hostile:
        p = cache_path(svc, {"group": "g1", "id": 7, "name": name})
        assert p.parent == svc._cache_dir, f"资源名 {name!r} 逃出了缓存目录: {p}"
    # 正常名保持原样，不因归一化而改名（缓存命中率不能被这次修复破坏）
# ---------- M7 follow-up: 并发 open 去重 ----------


def _race_materialize(svc, infos: list[dict]):
    """两个线程在同一道闸门后同时物化（SFTP 就是每连接一个线程）。

    返回 (results, errors)：results 按序号记返回路径，errors 记异常——异常
    不直接抛出，好让断言先看到"下载了几次"这条根因证据。
    """
    results: dict[int, object] = {}
    errors: dict[int, Exception] = {}
    barrier = threading.Barrier(len(infos), timeout=10)

    def _worker(rid: int, info: dict) -> None:
        try:
            barrier.wait()
            results[rid] = dio.materialize_to_cache(svc, info)
        except Exception as e:  # 回传到断言里，别让线程静默死掉
            errors[rid] = e

    threads = [
        threading.Thread(target=_worker, args=(rid, info))
        for rid, info in enumerate(infos)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "并发物化未在 15s 内返回（疑似死锁）"
    return results, errors


def test_concurrent_opens_download_the_resource_once(
    make_service, loop_thread, monkeypatch, tmp_path
):
    """M7 续：并发打开同一路径只应下载一次。

    SFTP 是每连接一个线程：两个客户端同时 open 同一文件，此前会各自走一遍
    「取 CDN 链接 → 下载 → 拷贝」——重复传输，并各留一份 recon_* 重组产物。
    """
    payload = b"payload-bytes" * 4096
    src = tmp_path / "recon_concurrent.bin"
    src.write_bytes(payload)
    calls: list[tuple] = []
    copies: list = []

    async def _download_info(group, rid):
        calls.append((group, rid))
        return (src.as_posix(), "concurrent.bin")

    svc = make_service(download_info=_download_info)
    svc._loop = loop_thread
    real_copy = dio._copy_into_cache

    def _slow_copy(sp, cache):
        copies.append(cache)
        time.sleep(0.3)  # 拉宽持锁窗口：另一个 open 必须在这期间到达
        real_copy(sp, cache)

    monkeypatch.setattr(dio, "_copy_into_cache", _slow_copy)
    info = {"group": "g1", "id": 0, "name": "f0", "size": len(payload)}
    results, errors = _race_materialize(svc, [dict(info), dict(info)])

    assert calls == [("g1", 0)], (
        f"并发 open 重复下载：download_info 被调用 {len(calls)} 次（应为 1 次）"
    )
    assert not errors, f"并发 open 抛异常：{errors}"
    expected = cache_path(svc, info)
    assert copies == [expected], f"重复拷贝：{copies}"
    assert results == {0: expected, 1: expected}, results
    assert expected.read_bytes() == payload


def test_the_cache_lock_is_per_path_not_global(
    make_service, loop_thread, monkeypatch, tmp_path
):
    """去重锁必须按缓存路径分片：两个不同资源必须能同时物化。

    一把全局锁同样能"修好"重复下载，却把 SFTP 吞吐压成单路——本用例钉粒度。
    """
    payload = b"payload-bytes" * 4096
    srcs = {}  # rid -> recon 源文件
    for rid in (0, 1):
        p = tmp_path / f"recon_{rid}.bin"
        p.write_bytes(payload)
        srcs[rid] = p

    async def _download_info(group, rid):
        return (srcs[rid].as_posix(), f"f{rid}")

    svc = make_service(download_info=_download_info)
    svc._loop = loop_thread
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()
    real_copy = dio._copy_into_cache

    def _slow_copy(sp, cache):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.3)  # 让两个物化窗口重叠
            real_copy(sp, cache)
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(dio, "_copy_into_cache", _slow_copy)
    infos = [
        {"group": "g1", "id": rid, "name": f"f{rid}", "size": len(payload)}
        for rid in (0, 1)
    ]
    results, errors = _race_materialize(svc, infos)

    assert not errors, f"并发物化抛异常：{errors}"
    assert state["peak"] == 2, (
        f"不同资源未能并行物化（峰值并发 {state['peak']}）——锁必须按缓存路径"
        "分片，全局锁会把 SFTP 吞吐压成单路"
    )
    assert results == {0: cache_path(svc, infos[0]), 1: cache_path(svc, infos[1])}


    assert cache_path(svc, {"group": "g1", "id": 0, "name": "f0"}).name == "g1_0_f0"


# ---------- L7: stat() 不再翻遍全群分页 ----------


class _SearchStore(_FakeStore):
    """真实 store 语义：keyword 是子串匹配（SQL LIKE %kw%），并记录每次查询。"""

    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.queries: list = []

    async def query_resources(self, q):
        self.queries.append((q.page, q.page_size, q.keyword))
        rows = [
            SimpleNamespace(id=i, name=f"f{i}", size=10 + i)
            for i in range(self.count)
        ]
        if q.keyword:
            rows = [r for r in rows if q.keyword in r.name]
        start = (q.page - 1) * q.page_size
        return SimpleNamespace(
            items=rows[start : start + q.page_size],
            total=len(rows),
            page=q.page,
            page_size=q.page_size,
        )


def test_find_row_uses_a_targeted_query(make_service, loop_thread):
    """L7: 命中关键字后不得再全量翻页（原来每次 stat 最多 200 次 SQL）。"""
    store = _SearchStore(1200)
    svc = make_service(store=store)
    svc._loop = loop_thread
    assert svc._find_row("g1", "f1100")["id"] == 1100
    assert len(store.queries) == 1 and store.queries[0][2] == "f1100", (
        "L7: 应按 (group_id, name) 直接查，而不是 collect_resources 翻遍全群"
    )


def test_find_row_caches_hits_briefly(make_service, loop_thread):
    """L7: 同一路径的重复 stat/open 不得重复查库。"""
    store = _SearchStore(3)
    svc = make_service(store=store)
    svc._loop = loop_thread
    assert svc._find_row("g1", "f1")["id"] == 1
    assert svc._find_row("g1", "f1")["id"] == 1
    assert len(store.queries) == 1, "L7: 短 TTL 内重复查找必须命中缓存"


# ---------- U2: _run_in_loop argument contract (P0 regression) ----------
#
# download_server_io.py passes coroutine OBJECTS (`_groups()`, `_files()`,
# `svc._download_info(...)`, `asyncio.to_thread(_fetch)`) while an older call
# site passed a zero-arg CALLABLE. `_run_in_loop` must accept both: when it did
# not, a misspelled call site scheduled nothing, the future never completed, and
# the failure surfaced 180s later as a bare TimeoutError that _find_row's caller
# swallowed -- so SFTP reported NO_SUCH_FILE for every cloud file. These tests
# pin the contract and the fail-fast behaviour.


def _loop_and_service():
    """Return (service, stop_fn): a real loop thread plus a service bound to it."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    svc = object.__new__(DownloadServerService)
    svc._loop = loop

    def _stop():
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    return svc, _stop


async def _returns_42():
    return 42


def test_run_in_loop_accepts_a_coroutine_object():
    """The shape every download_server_io.py call site uses."""
    svc, stop = _loop_and_service()
    try:
        started = time.monotonic()
        assert svc._run_in_loop(_returns_42()) == 42
        assert time.monotonic() - started < 5.0, (
            "a scheduled coroutine must complete promptly; the old bug surfaced "
            "as a 180s TimeoutError instead"
        )
    finally:
        stop()


def test_run_in_loop_accepts_a_zero_arg_callable():
    """The other shape in use; both must work so neither call site regresses."""
    svc, stop = _loop_and_service()
    try:
        assert svc._run_in_loop(_returns_42) == 42
    finally:
        stop()


def test_run_in_loop_rejects_non_awaitable_fast():
    """Fail fast instead of occupying the loop thread for the full timeout."""
    svc, stop = _loop_and_service()
    try:
        started = time.monotonic()
        with pytest.raises(TypeError):
            svc._run_in_loop(object(), timeout=180.0)
        assert time.monotonic() - started < 5.0
    finally:
        stop()
