"""L7 回归：SMB 停机语义。

背景（上游证据，impacket/smbserver.py @ master）：::

    class SimpleSMBServer:
        def start(self):
            self.__srvsServer.start()
            self.__wkstServer.start()
            self.__server.serve_forever()      # 阻塞

        def stop(self):
            self.__server.server_close()       # 唯一一句

而 ``self.__server`` 是::

    class SMBSERVER(socketserver.ThreadingMixIn, socketserver.TCPServer)

于是：
  * ``BaseServer.serve_forever()`` 的唯一退出条件是 ``__shutdown_request``，
    该标志只由 ``BaseServer.shutdown()`` 置位；
  * ``BaseServer.server_close()`` 在基类里是空实现 ``pass``；
  * ``ThreadingMixIn.daemon_threads`` 默认 False。

⇒ 旧代码 ``stop()`` + ``join(timeout=2.0)`` 停不掉线程、也关不掉端口。
本模块钉住修复后的契约：``stop_smb_server()`` 必须走
``getServer().shutdown()`` + ``server_close()`` 这一对真正的停机原语。
"""

import inspect
import types
from pathlib import Path

import pytest

from core.application import download_server_io as io_mod

ROOT = Path(__file__).resolve().parents[1]


class _FakeTCPServer:
    """按 socketserver.TCPServer 的真实接口构造的替身。"""

    def __init__(self):
        self.shutdown_called = 0
        self.server_close_called = 0
        import socket as _s

        self.socket = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]

    def shutdown(self):
        self.shutdown_called += 1

    def server_close(self):
        self.server_close_called += 1


class _FakeSimpleSMBServer:
    """SimpleSMBServer 的替身：只有 ``getServer()`` 与 ``stop()`` 是公共面。"""

    def __init__(self, raw):
        self.__server = raw
        self.stop_called = 0

    def getServer(self):
        return self.__server

    def stop(self):
        self.stop_called += 1
        # 复刻 impacket 的实现：仅调 server_close()
        self.__server.server_close()


class _Svc:
    """最小宿主：``stop_smb_server`` 只依赖 ``_smb_server``。"""

    def __init__(self, server):
        self._smb_server = server
        self._smb_sock = None


def _real_impacket_source_guard() -> bool:
    """若能拿到真实 impacket，就直接断言其 start/stop 的语义。"""
    pytest.importorskip("impacket.smbserver")
    return True


# ---------- 1. 上游语义（有真实 impacket 时才有意义） ----------


def test_upstream_stop_only_calls_server_close():
    """钉住上游事实：``SimpleSMBServer.stop()`` 只调 ``server_close()``。

    没有 impacket 时跳过——但下面的替身用例已经把同一契约钉死在本地，
    所以本条的 skipped 不会削弱覆盖。
    """
    if not _real_impacket_source_guard():
        return
    from impacket.smbserver import SimpleSMBServer

    src = inspect.getsource(SimpleSMBServer.stop)
    assert "server_close" in src, "impacket 改了 stop() 语义，L7 的前提需重审"
    assert "shutdown()" not in src, (
        "impacket 的 stop() 现在会调 shutdown() —— L7 的停机修复可以简化"
    )
    start_src = inspect.getsource(SimpleSMBServer.start)
    assert "serve_forever" in start_src, "start() 不再阻塞，L7 的编排需重审"


# ---------- 2. stop_smb_server 必须用真正的停机原语 ----------


def test_stop_smb_server_uses_the_real_teardown_primitives():
    raw = _FakeTCPServer()
    svc = _Svc(_FakeSimpleSMBServer(raw))

    io_mod.stop_smb_server(svc)

    assert raw.shutdown_called == 1, (
        "L7: 必须调 socketserver 的 shutdown() 才能让 serve_forever() 退出；"
        "只用 impacket 的 stop() 会耗尽 join 超时并留下占用端口的线程"
    )
    assert raw.server_close_called >= 1, "L7: 监听套接字必须被关掉"
    assert svc._smb_server is None, "L7: 句柄必须释放，否则 shutdown() 不可重入"


def test_stop_smb_server_falls_back_for_a_server_without_getserver():
    """没有 ``getServer()`` 的替身（旧版/裁剪版）退化为 ``stop()``，不得抛异常。"""
    server = types.SimpleNamespace(stop_called=0)

    def _stop():
        server.stop_called += 1

    server.stop = _stop
    svc = _Svc(server)

    io_mod.stop_smb_server(svc)  # 不得抛出

    assert server.stop_called == 1
    assert svc._smb_server is None


def test_stop_smb_server_is_a_noop_when_never_started():
    svc = _Svc(None)
    io_mod.stop_smb_server(svc)  # 不得抛出
    assert svc._smb_server is None


def test_stop_smb_server_swallows_a_failing_shutdown():
    """停机路径必须 best-effort：raw.shutdown() 抛异常也不能中断清理。"""

    class _Boom(_FakeTCPServer):
        def shutdown(self):
            self.shutdown_called += 1
            raise RuntimeError("boom")

    raw = _Boom()
    svc = _Svc(_FakeSimpleSMBServer(raw))

    io_mod.stop_smb_server(svc)  # 不得抛出

    assert raw.shutdown_called == 1
    assert raw.server_close_called == 1, "shutdown() 失败后仍须尝试 server_close()"
    assert svc._smb_server is None


# ---------- 3. 源码级守卫：旧写法不得回归 ----------


def test_shutdown_no_longer_uses_the_bare_stop():
    """``DownloadServerService.shutdown()`` 里不得再出现裸 ``_smb_server.stop()``。"""
    from core.application.download_server import DownloadServerService

    src = inspect.getsource(DownloadServerService.shutdown)
    assert "_smb_server.stop()" not in src, (
        "L7: 裸 stop() 不构成停机；必须经 stop_smb_server() "
        "(shutdown() + server_close())"
    )
    assert "stop_smb_server(self)" in src, "L7: 两条 SMB 停机路径必须收敛到同一点"


def test_shutdown_closes_the_smb_listening_socket():
    from core.application.download_server import DownloadServerService

    src = inspect.getsource(DownloadServerService.shutdown)
    assert "_close_socket(self._smb_sock)" in src, (
        "L7: 监听套接字必须经 _close_socket 释放（SHUT_RDWR 唤醒 accept 阻塞）"
    )


def test_sftp_and_smb_share_the_socket_closer():
    """同构性：SFTP 与 SMB 的收尾必须复用同一帮手，不得各写一份。"""
    from core.application import download_server as srv_mod
    from core.application.download_server import DownloadServerService

    assert inspect.isfunction(srv_mod._close_socket), "_close_socket 必须是模块级帮手"
    src = inspect.getsource(DownloadServerService.shutdown)
    assert "_close_socket(self._sftp_sock)" in src
    assert "_close_socket(self._smb_sock)" in src
    # 旧的内联写法（shutdown + close 各一份）不得回归
    assert src.count("socket.SHUT_RDWR") == 0, (
        "L7: 收尾应集中在 _close_socket，不得再内联一份 shutdown/close"
    )


def test_start_publishes_the_listening_socket():
    """``start_smb_server`` 必须把监听套接字回传，否则 shutdown 无从唤醒。"""
    src = inspect.getsource(io_mod.start_smb_server)
    assert "svc._smb_sock = server.getServer().socket" in src, (
        "L7: 监听套接字必须发布到 svc._smb_sock"
    )
    assert "svc._smb_sock = None" in src, (
        "L7: 失败分支必须把 _smb_sock 清成 None（不能留半构造状态）"
    )


def test_close_socket_is_idempotent_and_tolerates_none():
    from core.application.download_server import _close_socket

    _close_socket(None)  # 不得抛

    import socket as _s

    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _close_socket(s)
    _close_socket(s)  # 二次调用必须安全（shutdown 已关的 fd）
