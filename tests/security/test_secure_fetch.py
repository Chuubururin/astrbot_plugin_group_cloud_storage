"""secure_fetch 单一实现的行为钉子（P0d：SSRF 抓取收敛）。

历史上四份 async + 一份 sync 的手写重定向循环各自为政，任何一次 SSRF
加固都要改多处。本文件把收敛后的**唯一实现**钉死：

- 每一跳都做 resolve_and_pin_ip 复检（302 不能进内网 / DNS rebinding）；
- 重定向链有界；响应体流式封顶；
- 失败不留半文件（原子 .part 发布）；
- sync 变体（SFTP 线程用）与 async 语义一致。
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapters.external.secure_fetch import (
    FetchPolicy,
    RedirectLimitError,
    SecureFetchError,
    SizeLimitError,
    UrlRejectedError,
    fetch_bytes,
    fetch_to_file,
    fetch_to_file_sync,
)

PAYLOAD = b"SECURE-FETCH-" * 64


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return srv


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *a):
        pass


class _RedirectToPort(BaseHTTPRequestHandler):
    """302 -> 同一 host 的另一个端口（由 URL 尾巴指定）。"""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{self.path.strip('/')}/final")
        self.end_headers()

    def log_message(self, *a):
        pass


class _SelfRedirect(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        port = self.server.server_port
        self.send_header("Location", f"http://127.0.0.1:{port}/loop")
        self.end_headers()

    def log_message(self, *a):
        pass


class _RedirectNoLocation(BaseHTTPRequestHandler):
    """畸形 3xx：不带 Location 的 302。"""

    def do_GET(self):
        self.send_response(302)
        self.end_headers()

    def log_message(self, *a):
        pass


def _policy(**kw):
    kw.setdefault("max_bytes", 10 * 1024 * 1024)
    kw.setdefault("timeout", 10.0)
    return FetchPolicy(**kw)


# ---------- happy path / 封顶 / 原子发布 ----------

def test_fetch_to_file_writes_and_publishes(tmp_path):
    srv = _serve(_Ok)
    try:
        dest = tmp_path / "out.bin"
        n = asyncio.run(fetch_to_file(
            f"http://127.0.0.1:{srv.server_port}/f", dest,
            _policy(allow_private=True),
        ))
        assert n == len(PAYLOAD) and dest.read_bytes() == PAYLOAD
        assert not list(tmp_path.glob(".*.part")), "临时文件必须被清理"
    finally:
        srv.shutdown()


def test_fetch_bytes_caps_body_mid_stream(tmp_path):
    srv = _serve(_Ok)
    try:
        with pytest.raises(SizeLimitError):
            asyncio.run(fetch_bytes(
                f"http://127.0.0.1:{srv.server_port}/f",
                _policy(max_bytes=16, allow_private=True),
            ))
    finally:
        srv.shutdown()


def test_failed_fetch_leaves_no_partial_file(tmp_path):
    srv = _serve(_Ok)
    try:
        dest = tmp_path / "never.bin"
        with pytest.raises(SizeLimitError):
            asyncio.run(fetch_to_file(
                f"http://127.0.0.1:{srv.server_port}/f", dest,
                _policy(max_bytes=8, allow_private=True),
            ))
        assert not dest.exists(), "半文件不得出现在最终路径（原子发布）"
        assert not list(tmp_path.glob(".*.part"))
    finally:
        srv.shutdown()


# ---------- 重定向的逐跳复检（SSRF 核心不变量） ----------

def test_redirect_to_untrusted_port_is_rejected_per_hop(tmp_path):
    """入口跳靠 trusted_origins 放行；302 指向**另一个**私有端口必须被
    下一跳复检拒绝——旧 sync 实现靠"直接拒绝一切重定向"作弊，既误伤
    合法 CDN 又表达不了本不变量。"""
    entry = _serve(_RedirectToPort)
    target = _serve(_Ok)
    try:
        trusted = frozenset({("127.0.0.1", entry.server_port)})
        with pytest.raises(UrlRejectedError):
            asyncio.run(fetch_to_file(
                f"http://127.0.0.1:{entry.server_port}/{target.server_port}",
                tmp_path / "nope.bin",
                _policy(allow_private=False, trusted_origins=trusted),
            ))
    finally:
        entry.shutdown()
        target.shutdown()


def test_redirect_chain_followed_when_allowed(tmp_path):
    entry = _serve(_RedirectToPort)
    target = _serve(_Ok)
    try:
        dest = tmp_path / "r.bin"
        n = asyncio.run(fetch_to_file(
            f"http://127.0.0.1:{entry.server_port}/{target.server_port}",
            dest, _policy(allow_private=True),
        ))
        assert n == len(PAYLOAD) and dest.read_bytes() == PAYLOAD
    finally:
        entry.shutdown()
        target.shutdown()


def test_redirect_loop_bounded(tmp_path):
    srv = _serve(_SelfRedirect)
    try:
        dest = tmp_path / "loop.bin"
        with pytest.raises(RedirectLimitError):
            asyncio.run(fetch_to_file(
                f"http://127.0.0.1:{srv.server_port}/loop",
                dest, _policy(allow_private=True, max_redirects=2),
            ))
    finally:
        srv.shutdown()


def test_redirect_without_location_is_error(tmp_path):
    """回归钉：无 Location 的 3xx 必须报错。httpx 的 is_redirect 要求有
    Location，raise_for_status 又不覆盖 3xx——两头都不管时，畸形重定向的
    空响应体曾被当作成功载荷返回。"""
    srv = _serve(_RedirectNoLocation)
    try:
        dest = tmp_path / "nl.bin"
        with pytest.raises(SecureFetchError):
            asyncio.run(fetch_to_file(
                f"http://127.0.0.1:{srv.server_port}/f", dest,
                _policy(allow_private=True),
            ))
        assert not dest.exists(), "失败不得留下最终文件"
        with pytest.raises(SecureFetchError):
            asyncio.run(fetch_bytes(
                f"http://127.0.0.1:{srv.server_port}/f",
                _policy(allow_private=True),
            ))
        with pytest.raises(SecureFetchError):
            fetch_to_file_sync(
                f"http://127.0.0.1:{srv.server_port}/f", dest,
                _policy(allow_private=True),
            )
    finally:
        srv.shutdown()


# ---------- 错误类型契约 ----------

@pytest.mark.parametrize("exc_cls", [
    UrlRejectedError, RedirectLimitError, SizeLimitError,
])
def test_all_errors_are_value_error_compatible(exc_cls):
    """四个历史调用点的异常契约：全部 ValueError 族（webapi/handler 的
    except ValueError 语义不变）。"""
    assert issubclass(exc_cls, SecureFetchError)
    assert issubclass(exc_cls, ValueError)


# ---------- sync 变体（SFTP 线程） ----------

def test_sync_variant_same_security_semantics(tmp_path):
    srv = _serve(_Ok)
    try:
        dest = tmp_path / "s.bin"
        n = fetch_to_file_sync(
            f"http://127.0.0.1:{srv.server_port}/f", dest,
            _policy(allow_private=True),
        )
        assert n == len(PAYLOAD) and dest.read_bytes() == PAYLOAD
        with pytest.raises(UrlRejectedError):
            fetch_to_file_sync(
                f"http://127.0.0.1:{srv.server_port}/f", dest,
                _policy(allow_private=False),
            )
    finally:
        srv.shutdown()
