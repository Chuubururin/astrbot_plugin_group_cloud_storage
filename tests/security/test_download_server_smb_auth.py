"""M14: SMB 通道必须校验 download_token，且 share 声明为只读。

http/sftp 都用 download_token 认证；SMB 分支此前只 SimpleSMBServer +
addShare，没有 addCredential，也没把 share 设为只读——一旦
download_server_host 不是 127.0.0.1，token 在该通道上完全失效。

替身按 impacket 0.13 的**真实签名**构造（addCredential 4 个必填参数、
readOnly 是字符串、没有 setLogHim），不再从生产调用点反推形状；另有一个
pytest.importorskip("impacket") 守护的用例直接打真实 SimpleSMBServer。
"""

from __future__ import annotations

import logging
import shutil
import struct
import sys
import tempfile
import types

import pytest

from core.application.download_server import DownloadServerService
from core.application.download_server_io import configure_smb_server

TOKEN = "s3cret-token"
# NT hash = MD4(UTF-16LE(password))。impacket 的 addCredential 收的是 NTLM hash，
# 不是明文口令；下面这条向量由官方算法锚定（MD4("password") =
# 8846f7eaee8fb117ad06bdd830b7586c）。
NT_HASH_OF_TOKEN = "f3bcf3035dc2ec6c9b91291c3bca0f35"


def _md4(data: bytes) -> bytes:
    """纯 python MD4（NT hash 的核心）：无 impacket 的环境也能断言 hash 本身。"""

    def rol(x: int, n: int) -> int:
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    msg = data + b"\x80"
    msg += b"\x00" * ((56 - len(msg) % 64) % 64)
    msg += struct.pack("<Q", (len(data) * 8) & 0xFFFFFFFFFFFFFFFF)
    for off in range(0, len(msg), 64):
        x = list(struct.unpack("<16I", msg[off : off + 64]))
        a, b, c, d = h
        for i in range(16):
            a = rol((a + ((b & c) | (~b & d)) + x[i]) & 0xFFFFFFFF, (3, 7, 11, 19)[i % 4])
            a, b, c, d = d, a, b, c
        for i in range(16):
            k = (i % 4) * 4 + i // 4
            g = (b & c) | (b & d) | (c & d)
            a = rol((a + g + x[k] + 0x5A827999) & 0xFFFFFFFF, (3, 5, 9, 13)[i % 4])
            a, b, c, d = d, a, b, c
        for i, k in enumerate((0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)):
            a = rol((a + (b ^ c ^ d) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, (3, 9, 11, 15)[i % 4])
            a, b, c, d = d, a, b, c
        h = [(u + v) & 0xFFFFFFFF for u, v in zip(h, (a, b, c, d), strict=True)]
    return struct.pack("<4I", *h)


def _nt_hash(password: str) -> bytes:
    return _md4(str(password).encode("utf-16le"))


class _FakeNTLM:
    """impacket.ntlm 的 hash 派生入口（真实实现分别是 DES 与 MD4）。"""

    @staticmethod
    def compute_nthash(password):
        return _nt_hash(password)

    @staticmethod
    def compute_lmhash(password):
        # LM hash 需要 DES；这里只锁定「派生成 16 字节 hash」的形状契约。
        return _md4(str(password).upper().encode("latin-1", "replace"))[:16]


def _configparser_str(value):
    """ConfigParser.set() 只接受字符串，其余类型直接 TypeError（真实实现如此）。"""
    if not isinstance(value, str):
        raise TypeError(f"option values must be strings, got {type(value).__name__}")
    return value


class _ContractSMBServer:
    """按 impacket smbserver.py 真实签名实现的替身。

    - addCredential(name, uid, lmhash, nthash)：4 个必填位置参数
    - addShare(shareName, sharePath, shareComment='', shareType='0', readOnly='no')：
      readOnly 原样交给 ConfigParser.set()，读取端 5 处比较 == "yes"
    - 只有 setLogFile()，没有 setLogHim()
    """

    def __init__(self, listenAddress=None, listenPort=None):
        self.listen = (listenAddress, listenPort)
        self.credentials: dict[str, tuple] = {}
        self.shares: dict[str, dict] = {}
        self.started = False
        self.stopped = False

    def addCredential(self, name, uid, lmhash, nthash):
        # 真实实现会把 hex 还原成字节；这里保留原始字符串供断言。
        self.credentials[str(name).lower()] = (uid, lmhash, nthash)

    def addShare(self, shareName, sharePath, shareComment="", shareType="0", readOnly="no"):
        share = str(shareName).upper()
        self.shares[share] = {
            "comment": _configparser_str(shareComment),
            "read only": _configparser_str(readOnly),
            "share type": _configparser_str(shareType),
            "path": _configparser_str(sharePath),
        }

    def setLogFile(self, logFile):
        self.log_file = logFile

    def getCredentials(self):
        return self.credentials

    def is_read_only(self, share_name) -> bool:
        """读取端 5 处比较：== "yes"。"""
        return self.shares[str(share_name).upper()]["read only"] == "yes"

    def authenticate(self, user, password) -> bool:
        """复刻 impacket smbserver.py 的 AUTHENTICATE_MESSAGE 分支：凭据表为空时
        放行任何人（原缺陷），非空时用户名必须命中，否则 STATUS_LOGON_FAILURE
        ——匿名会话（user_name=""）同样命中不了。"""
        if not self.credentials:
            return True
        entry = self.credentials.get(str(user or "").lower())
        if entry is None:
            return False
        _uid, _lmhash, nthash = entry
        return nthash == _nt_hash(password).hex()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _build_service(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "cloudstorage-")
    )
    cfg = {
        "download_server_enabled": True,
        "download_token": TOKEN,
        "download_server_host": "0.0.0.0",
        "download_smb_port": 1445,
    }
    cfg.update(overrides)
    return DownloadServerService(None, cfg)


@pytest.fixture
def smb_env(tmp_path, monkeypatch):
    """装一个按真实签名构造的假 impacket，使 SMB 通道真的被启用。"""
    created: list[_ContractSMBServer] = []
    fake = types.ModuleType("impacket")
    smbserver = types.ModuleType("impacket.smbserver")
    ntlm = types.ModuleType("impacket.ntlm")

    class _Server(_ContractSMBServer):
        def __init__(self, listenAddress=None, listenPort=None):
            super().__init__(listenAddress, listenPort)
            created.append(self)

    smbserver.SimpleSMBServer = _Server
    ntlm.compute_lmhash = _FakeNTLM.compute_lmhash
    ntlm.compute_nthash = _FakeNTLM.compute_nthash
    fake.smbserver = smbserver
    fake.ntlm = ntlm
    monkeypatch.setitem(sys.modules, "impacket", fake)
    monkeypatch.setitem(sys.modules, "impacket.smbserver", smbserver)
    monkeypatch.setitem(sys.modules, "impacket.ntlm", ntlm)

    svc = _build_service(tmp_path, monkeypatch)
    assert svc.smb_available is True, "假 impacket 可导入，SMB 通道应启用"
    yield svc, created
    if svc._cache_root is not None:
        shutil.rmtree(svc._cache_root, ignore_errors=True)


def _start_smb(svc):
    svc._start_smb()
    svc._smb_thread.join(timeout=5)
    assert not svc._smb_thread.is_alive(), "SMB 线程未结束（假 server.start() 不阻塞）"


def test_nt_hash_helper_matches_the_official_ntlm_vector():
    """锚定替身里的 MD4，否则 NT_HASH_OF_TOKEN 无法作为契约向量。"""
    assert _nt_hash("password").hex() == "8846f7eaee8fb117ad06bdd830b7586c"
    assert _nt_hash(TOKEN).hex() == NT_HASH_OF_TOKEN


def test_smb_credentials_follow_the_real_impacket_signature(smb_env):
    """H4: addCredential 必须收到 4 个参数，且后两个是 NTLM hash（不是口令）。"""
    svc, created = smb_env
    _start_smb(svc)
    assert created, "SMB server 未构造：bootstrap 异常被吞掉了"
    creds = created[0].getCredentials()
    assert list(creds) == ["cloud"], (
        "M14: 必须以固定用户注册凭据（否则 impacket 放行任何会话）"
    )
    uid, lmhash, nthash = creds["cloud"]
    assert uid == 0
    for label, value in (("lmhash", lmhash), ("nthash", nthash)):
        assert isinstance(value, str) and len(value) == 32, f"H4: {label} 必须是 hex 字符串"
        int(value, 16)
    assert nthash == NT_HASH_OF_TOKEN, "H4: 口令必须先派生为 NT hash 再交给 impacket"
    assert TOKEN not in (lmhash, nthash), "H4: 不得把明文口令当 hash 传进去"


def test_smb_share_declares_read_only_as_the_string_yes(smb_env):
    """H5: readOnly 必须是字符串 "yes"——bool 既抛 TypeError 也永不生效。"""
    svc, created = smb_env
    _start_smb(svc)
    server = created[0]
    share = server.shares["CLOUD"]
    assert share["path"] == svc._smb_dir.as_posix()
    assert share["read only"] == "yes"
    assert server.is_read_only("cloud") is True, (
        'H5: impacket 读取端比较 == "yes"，只读声明必须真的生效'
    )


def test_smb_anonymous_and_wrong_password_sessions_are_rejected(smb_env):
    """M14: 匿名（空用户名）与错误口令的会话必须被拒。"""
    svc, created = smb_env
    _start_smb(svc)
    server = created[0]
    assert server.credentials, "M14: 凭据表为空时 impacket 会放行任何会话"
    assert server.authenticate("", "") is False, "M14: 匿名会话必须被拒"
    assert server.authenticate("cloud", "") is False, "M14: 空口令必须被拒"
    assert server.authenticate("cloud", "wrong-token") is False
    assert server.authenticate("cloud", TOKEN) is True


def test_smb_credentials_match_the_sftp_channel(smb_env):
    """M14: SMB 与 SFTP 必须共用同一组凭据（同一个 download_token）。"""
    svc, _created = smb_env
    user, password = svc.smb_credentials()
    assert password == svc.token
    assert svc.sftp_info()["user"] == user
    assert svc.sftp_info()["password"] == password


async def test_smb_server_instance_is_published_and_stopped(smb_env):
    """M12: 实例必须挂到 svc._smb_server，否则 shutdown() 的 stop() 是死代码。"""
    svc, created = smb_env
    _start_smb(svc)
    assert created and svc._smb_server is created[0], (
        "M12: SimpleSMBServer 只活在 _serve() 局部变量里，stop() 永远不会执行"
    )
    assert created[0].started is True
    await svc.shutdown()
    assert created[0].stopped is True, "M12: shutdown() 必须停掉 SMB server"


def test_smb_bootstrap_failure_is_reported_at_error_level(smb_env, monkeypatch, caplog):
    """M12: 配置/启动失败必须报错，不能只是一条被忽略的 warning。"""
    svc, _created = smb_env

    def _boom(*args, **kwargs):
        raise RuntimeError("impacket: bad config")

    monkeypatch.setattr(_ContractSMBServer, "addShare", _boom)
    with caplog.at_level(logging.ERROR):
        svc._start_smb()
        svc._smb_thread.join(timeout=5)
    assert svc._smb_server is None, "M12: 失败后不得留下半初始化的实例"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "M12: SMB 配置失败必须留下 ERROR 级日志"
    )


def test_configure_smb_server_matches_real_impacket(tmp_path, monkeypatch):
    """T3: 真实 impacket（只构造、不 start()）直接调 configure_smb_server。

    impacket 未安装时本用例 skip——这正是「假 impacket 掩盖真实契约」的残留风险。
    """
    pytest.importorskip("impacket")
    from impacket.ntlm import compute_nthash
    from impacket.smbserver import SimpleSMBServer

    svc = _build_service(tmp_path, monkeypatch)
    server = SimpleSMBServer(listenAddress="127.0.0.1", listenPort=1445)
    configure_smb_server(server, svc)  # 修复前：TypeError（addCredential 少 2 个参数）

    _uid, _lmhash, nthash = server.getCredentials()["cloud"]
    assert nthash == compute_nthash(TOKEN), "H4: 注册的必须是 token 的 NT hash"
    # SimpleSMBServer 没有公开的 share 读取接口，只能看它写进 ConfigParser 的值。
    cfg = server._SimpleSMBServer__smbConfig
    assert cfg.get("CLOUD", "read only") == "yes", "H5: 只读声明必须是字符串 yes"
