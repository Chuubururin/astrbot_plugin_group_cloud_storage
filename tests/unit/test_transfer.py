"""TransferService 测试（v2.13：导出链路已切除，仅导入拉取管线）。

sftp/smb 库层打桩；http 用本地 HTTP 服务器实测（GET）。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.transfer import TransferService  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    queue = OpQueue(lambda op: None, interval=0.0)
    await queue.start()
    svc = TransferService(store, queue, tmp_path / "tmp", config={
        "fetch_max_bytes": 10 * 1024 * 1024, "fetch_timeout_sec": 10,
        "transfer_timeout_sec": 10,
        # 本地 mock 服务器监听 127.0.0.1，需显式放行私有地址（生产默认拒绝）
        "fetch_allow_private_address": True,
    })
    yield tmp_path, store, queue, svc
    await queue.shutdown()
    await store.close()


# ---------- URL 解析 ----------

def test_parse_target():
    t = TransferService.parse_target("sftp://user:p%40ss@host:2222/pub/a.bin")
    assert t["scheme"] == "sftp" and t["host"] == "host" and t["port"] == 2222
    assert t["user"] == "user" and t["password"] == "p@ss" and t["path"] == "/pub/a.bin"
    t = TransferService.parse_target("smb://host/share/dir/f.txt")
    assert t["scheme"] == "smb" and t["host"] == "host"
    assert t["path"] == "/share/dir/f.txt"


# ---------- ingress：多协议拉取 ----------

def test_download_to_http(env):
    tmp_path, store, queue, svc = env
    payload = b"HTTP-RELAY-DATA" * 100

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dest = tmp_path / "dl.bin"
        n = asyncio.run(svc.download_to(
            f"http://127.0.0.1:{srv.server_port}/x.bin", dest))
        assert n == len(payload) and dest.read_bytes() == payload
    finally:
        srv.shutdown()


def test_download_to_sftp_stub(env, monkeypatch):
    tmp_path, store, queue, svc = env
    captured = {}

    class FakeSFTPClient:
        # SSH 加密通道适配器：sftp:// 经 paramiko SSH 传输，
        # get() 为按需拉取（下载到本地暂存）。
        def get(self, remote, local): captured["remote"] = remote
        def close(self): pass

    class FakeSSHClient:
        def __init__(self): pass
        def load_system_host_keys(self): pass
        def set_missing_host_key_policy(self, policy): pass
        def connect(self, host, port, username, password, timeout, **kw):
            captured["host"] = host; captured["port"] = port
        def open_sftp(self): return FakeSFTPClient()
        def close(self): pass

    import paramiko  # 库层打桩依赖 paramiko（与 sftp 适配器同一可选依赖）
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"SFTPDATA")
    n = asyncio.run(svc.download_to(
        "sftp://u:p@127.0.0.1:2222/file.bin", dest))
    assert captured["host"] == "127.0.0.1" and captured["port"] == 2222


def test_download_to_smb_stub(env, monkeypatch):
    # 插件零第三方依赖（HL-12）：smb 适配器库层惰性导入；打桩测试在宿主
    # 未装 pysmb 时跳过（smb 导入需要宿主自装 pysmb，运行时才激活）
    pytest.importorskip("smb")
    tmp_path, store, queue, svc = env
    captured = {}

    class FakeConn:
        def __init__(self, *a, **kw): pass
        def connect(self, host, port, timeout): captured["host"] = host; return True
        def retrieveFile(self, share, path, fh, timeout):
            captured["share"] = share; captured["path"] = path
            fh.write(b"SMBDATA")
        def close(self): pass

    monkeypatch.setattr("smb.SMBConnection.SMBConnection", FakeConn)
    dest = tmp_path / "s.bin"
    n = asyncio.run(svc.download_to("smb://u:p@h/share/dir/f.bin", dest))
    assert n == 7 and dest.read_bytes() == b"SMBDATA"
    assert captured == {"host": "h", "share": "share", "path": "dir/f.bin"}


def test_download_unsupported_scheme(env):
    tmp_path, store, queue, svc = env
    with pytest.raises(ValueError):
        asyncio.run(svc.download_to("ldap://h/x", tmp_path / "x"))


