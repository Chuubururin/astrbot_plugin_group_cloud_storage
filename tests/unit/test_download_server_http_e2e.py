"""U5: `download_server` 的 **HTTP 端点**整合覆盖。

## 为什么要单独建这个文件

`serve_local_file` / `parse_range` 的**函数级**行为在
`test_download_server_regressions.py` 里已有 15 条用例（10 条 `parse_range`
参数化 + 5 条 `serve_local_file`），覆盖了 206 / 200 / 416 / clamp / 多段
fail-open / drain 超时 / recon 回收。

但 `_handle_http()` 这个**端点本身从未被测过**：既有的 5 条都是直接调
`serve_local_file(writer, ...)`，绕过了读头、路由、鉴权、参数解析、以及
302 与 500 两条分支。本文件只补这一段，**刻意不重复**上面那 15 条的语义。

## 覆盖矩阵

| # | 用例 | 锁住的行为 | 反向验证 |
| --- | --- | --- | --- |
| 1 | health / 404 路由 | 端点入口 | — |
| 2 | 鉴权 401 | token 校验 | — |
| 3 | 参数 400 | group/id 校验 | — |
| 4 | **302 云直链** | 单文件 target 是 URL → 免代理跳转 | 删 302 分支 |
| 5 | **206 经端点透传** | `headers["range"]` 正确下传 | 端点不传 range |
| 6 | 200 无 Range | 端点默认路径 | — |
| 7 | **500 兜底** | 异常不外泄、头已发则不双写 | 删 `_reply(500)` |
| 8 | recon_* 端到端回收 | 端点 + 文件层的清理时机 | — |
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from core.application.download_server import DownloadServerService

TOKEN = "t0ken"


# ---------- 真实 HTTP 客户端（不 mock StreamReader） ----------


async def _request(
    port: int,
    raw_target: str,
    *,
    method: str = "GET",
    range_header: str | None = None,
) -> bytes:
    """向真实端点发一次请求，读完整份响应（Connection: close）。

    走真正的 TCP 栈，因此 `readuntil(b"\\r\\n\\r\\n")` 的超时路径、
    头解析、以及分批写出都被真实执行。

    `range_header` 必须真的写进请求头：这是「端点把 headers["range"] 下传」
    那条断言的**唯一**输入通道，漏掉它用例就永远测不到 206。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        head = f"{method} {raw_target} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        if range_header is not None:
            head += f"Range: {range_header}\r\n"
        head += "\r\n"
        writer.write(head.encode("latin-1"))
        await writer.drain()
        chunks = []
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=10.0)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
async def http_server(tmp_path, monkeypatch):
    """起一个真实的 download_server HTTP 端点，yield (port, svc)。

    坑位记录：`start()` 只在 `http_port > 0` 时才绑定（`test_openlist.py` 的
    `test_guard_requires_port` 把这个语义锁死了），所以**不能**照搬 SFTP 那套
    `port=0` 让内核分配端口的写法——那样 `_http_server` 恒为 None。

    也不能用 `port=1` 之类的「大于 0 的假端口」：非 root 下 bind `127.0.0.1:1`
    会直接 `PermissionError`。

    正解是把 `asyncio.start_server` 换成一个**只记录调用、不做套接字绑定**的
    探针，让 `start()` 的启用/鉴权/端口守卫逻辑完整走一遍（这才是被测对象），
    再把**真实**的 `self._handle_http` 绑到内核分配的端口上。
    """
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "cloudstorage-http")
    )
    created: list[DownloadServerService] = []
    real_start_server = asyncio.start_server

    async def _start(store=None, download_info=None, config=None):
        cfg = {
            "download_server_enabled": True,
            "download_token": TOKEN,
            "download_http_port": 1,  # >0 只为了让 start() 走完守卫
            "download_sftp_port": 0,
            "download_smb_port": 0,
        }
        cfg.update(config or {})
        svc = DownloadServerService(store, cfg, download_info=download_info)
        created.append(svc)

        # 让 start() 里的 start_server 变成空操作，避免真的去 bind 特权端口。
        # 必须是 async 的：start() 里写的是 `await asyncio.start_server(...)`，
        # 返回 None 会炸 `TypeError: object NoneType can't be used in 'await'`。
        async def _noop_start_server(*a, **kw):
            return None

        monkeypatch.setattr(asyncio, "start_server", _noop_start_server)
        try:
            await svc.start()
        finally:
            monkeypatch.setattr(asyncio, "start_server", real_start_server)

        # 端点还是那个端点（self._handle_http），只是换内核分配的端口
        svc._http_server = await real_start_server(svc._handle_http, "127.0.0.1", 0)
        port = svc._http_server.sockets[0].getsockname()[1]
        return port, svc

    yield _start

    for svc in created:
        try:
            await svc.shutdown()
        except Exception:
            pass


def _head(code_and_reason: bytes) -> str:
    return code_and_reason.split(b"\r\n", 1)[0].decode("latin-1")


# ---------- 1–3: 路由 / 鉴权 / 参数 ----------


async def test_endpoint_routes_health_and_rejects_unknown_paths(http_server):
    """`/health` 免鉴权；其余未知路径一律 404（路由否定路径）。"""
    port, _ = await http_server()

    resp = await _request(port, "/health")
    assert _head(resp) == "HTTP/1.1 200 OK"
    assert resp.endswith(b"ok")

    for bad in ("/", "/nope", "/download2?token=" + TOKEN):
        assert _head(await _request(port, bad)) == "HTTP/1.1 404 Not Found", bad


async def test_endpoint_requires_the_token(http_server):
    """token 缺失或被篡改 -> 401；且不得泄漏资源是否存在。"""
    port, _ = await http_server()

    for target in ("/download", "/download?token=wrong&group=g1&id=1"):
        assert _head(await _request(port, target)) == "HTTP/1.1 401 Unauthorized", target

    # 非 GET 即使 token 正确也是 404（方法不在路由里）
    ok = f"/download?token={TOKEN}&group=g1&id=1"
    assert _head(await _request(port, ok, method="POST")) == "HTTP/1.1 404 Not Found"


async def test_endpoint_validates_group_and_id(http_server):
    """`group` 缺失或 `id` 非数字 -> 400（不是 404、也不是 500）。"""
    port, _ = await http_server()

    for target in (
        f"/download?token={TOKEN}",
        f"/download?token={TOKEN}&group=g1",
        f"/download?token={TOKEN}&group=g1&id=abc",
        f"/download?token={TOKEN}&id=1",
    ):
        assert _head(await _request(port, target)) == "HTTP/1.1 400 Bad Request", target


# ---------- 4: 302 云直链（既有零覆盖） ----------


async def test_endpoint_redirects_to_the_proxied_cdn_link_when_no_local_file(
    http_server, tmp_path
):
    """单文件 -> 302 到**本机代理链**（不是裸 CDN 直链），CDN 藏在 token 后面。

    注意 `download_info` 返回的是 **(download target, file name)**，
    target 是**两义**的（`core/application/files/download.py::download_info`）：

      - 单文件：target 就是 CDN 直链 URL，`Path(url).exists()` 为假 -> 走代理
      - 分卷：  target 是本地重组出来的 recon_* 文件 -> 走 `serve_local_file`

    Issue #8：早先这里 302 到**裸** CDN 直链。302 无法携带
    `Content-Disposition`，而 QQ CDN 的 URL 尾段是规格段（/0 /400 /800），
    于是 OpenList 的 SimpleHttp 回退到 `resp.Request.URL.Path` 落盘成数字名。
    现在改为：把 CDN 链接注册进代理表，302 到**本机** `?proxy=<token>`；
    客户端跟随重定向后命中 `serve_proxy()`，由它给出正确的文件名。

    反向验证：把 `register_proxy` 换回裸 `src`，本用例变红。
    """
    cdn = "https://cdn.example.com/qq/direct.bin"

    async def _download_info(group, rid):
        return (cdn, "direct.bin")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=7")

    assert _head(resp) == "HTTP/1.1 302 Found"
    # The redirect must NOT hand the bare CDN link to the client (that is the
    # bug); it must point at our own proxy endpoint.
    assert f"Location: {cdn}".encode() not in resp, "bare CDN link leaked"
    assert b"Location: http://127.0.0.1:" in resp, resp
    assert b"/download?proxy=" in resp, resp
    assert b"Content-Length: 0" in resp


async def test_endpoint_302_target_actually_serves_the_real_filename(
    http_server, monkeypatch
):
    """跟完这一跳：302 的目标必须真的给出带真实文件名的 Content-Disposition。

    这是 F-3 的端到端证明 —— OpenList 的 SimpleHttp 就是这么走的：
    GET 源 URL -> 跟随 302 -> 读**最终**响应的 Content-Disposition。
    只断言「302 指向代理」还不够，必须证明那一跳的响应对得上。
    出站 CDN 取回被 stub（测试不联网）。
    """
    import core.application.download_proxy as dpx

    # Literal public IP: the SSRF guard (assert_fetch_url_allowed) runs for
    # real on this hop and does a DNS lookup, so a hostname would be rejected
    # ("DNS resolution failed") before the stub client is ever reached. A
    # literal public IP passes the guard with no DNS, exactly as in
    # tests/contract/test_download_proxy.py.
    cdn = "https://1.2.3.4/qq/direct.bin"

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}

        def raise_for_status(self):
            return None

        async def aclose(self):
            return None

        async def aiter_bytes(self, n):
            yield b"PAY"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def build_request(self, method, url, headers=None):
            return (method, url, headers or {})

        async def send(self, req, stream=True):
            return _Resp()

    monkeypatch.setattr(dpx, "_open_client", lambda: _Client())

    async def _download_info(group, rid):
        return (cdn, "报告_2026.pdf")

    port, _ = await http_server(download_info=_download_info)
    first = await _request(port, f"/download?token={TOKEN}&group=g1&id=7")
    assert _head(first) == "HTTP/1.1 302 Found"

    # Follow the redirect the way a real client does.
    loc = None
    for line in first.split(b"\r\n"):
        if line.lower().startswith(b"location:"):
            loc = line.split(b":", 1)[1].strip().decode("latin-1")
    assert loc, first
    assert cdn not in loc, f"bare CDN link leaked into Location: {loc}"

    # Turn the absolute Location into a path+query for the fixed test port.
    from urllib.parse import urlsplit

    parts = urlsplit(loc)
    final = await _request(port, parts.path + ("?" + parts.query if parts.query else ""))
    assert _head(final) == "HTTP/1.1 200 OK", final
    head = final.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    disp = [
        ln.split(":", 1)[1].strip()
        for ln in head.split("\r\n")
        if ln.lower().startswith("content-disposition:")
    ]
    assert disp, head
    value = disp[0]
    # The whole point: a usable legacy name (extension preserved) AND the
    # encoded real name, instead of a spec segment like "802".
    assert 'filename="' in value, value
    assert "filename*=UTF-8''" in value, value
    assert "_2026.pdf" in value, value
    assert "download" not in value.replace("%E6%8E%A2", ""), value


async def test_endpoint_turns_a_rejected_download_info_into_404(http_server):
    """`download_info` 抛 ValueError（未知群/未就绪）-> 404，**不是** 500。

    端点专门捕了 ValueError 走 404；若退回通用兜底就会变成 500 并掩盖原因。
    """

    async def _download_info(group, rid):
        raise ValueError("volumes not ready")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=7")
    assert _head(resp) == "HTTP/1.1 404 Not Found"


# ---------- 5–8: 经端点的本地文件服务 ----------


async def test_endpoint_forwards_range_and_serves_206(http_server, tmp_path):
    """端点必须把 `Range` 头**下传**给 `serve_local_file`，并透传 206。

    反向验证：把 `headers.get("range")` 改成 `None`，本用例变红（会回 200 全量）。
    """
    src = tmp_path / "recon_e2e_clip.mp4"
    src.write_bytes(b"0123456789")

    async def _download_info(group, rid):
        return (src.as_posix(), "clip.mp4")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(
        port, f"/download?token={TOKEN}&group=g1&id=1", range_header="bytes=2-5"
    )

    assert _head(resp) == "HTTP/1.1 206 Partial Content"
    assert b"Content-Range: bytes 2-5/10" in resp
    assert resp.endswith(b"2345"), "body 必须是请求的那 4 个字节"
    assert not src.exists(), "端到端：recon_* 响应后必须被回收"


async def test_endpoint_serves_the_full_body_without_a_range(http_server, tmp_path):
    """无 Range -> 200 全量，且带 `Accept-Ranges: bytes`（供客户端后续切片）。"""
    src = tmp_path / "recon_e2e_full.bin"
    src.write_bytes(b"abcdef")

    async def _download_info(group, rid):
        return (src.as_posix(), "full.bin")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=1")

    assert _head(resp) == "HTTP/1.1 200 OK"
    assert b"Accept-Ranges: bytes" in resp
    assert resp.endswith(b"abcdef")


async def test_endpoint_answers_416_for_an_out_of_range_request(http_server, tmp_path):
    """越界 Range -> 416，且带 `Content-Range: bytes */total`。"""
    src = tmp_path / "recon_e2e_small.bin"
    src.write_bytes(b"abc")

    async def _download_info(group, rid):
        return (src.as_posix(), "small.bin")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=1")

    assert _head(resp) == "HTTP/1.1 200 OK"  # 无 Range 时是 200
    # 再来一次带越界 Range 的
    src2 = tmp_path / "recon_e2e_small2.bin"
    src2.write_bytes(b"abc")

    async def _download_info2(group, rid):
        return (src2.as_posix(), "small2.bin")

    port2, _ = await http_server(download_info=_download_info2)
    resp2 = await _request(
        port2, f"/download?token={TOKEN}&group=g1&id=1", range_header="bytes=99-"
    )

    assert _head(resp2) == "HTTP/1.1 416 Range Not Satisfiable"
    assert b"Content-Range: bytes */3" in resp2


async def test_endpoint_returns_500_when_the_handler_explodes(http_server):
    """处理中抛**非 ValueError** 异常 -> 500 `internal error`（不泄漏堆栈）。

    端点在自己发响应前就炸了，所以走的是完整 `_reply(500)` 路径。
    """

    async def _download_info(group, rid):
        raise RuntimeError("kaboom - must not reach the client")

    port, _ = await http_server(download_info=_download_info)
    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=1")

    assert _head(resp) == "HTTP/1.1 500 Internal Error"
    assert resp.endswith(b"internal error")
    assert b"kaboom" not in resp, "内部异常信息不得外泄"


async def test_endpoint_500_does_not_append_to_an_already_sent_response(
    http_server, tmp_path, monkeypatch
):
    """头已发出后再炸：兜底 `_reply(500)` 不得把第二个响应头追加进同一个 body。

    这是 `_handle_http` 里 `except: try: _reply(500)` 的真实意义——若它
    盲目改写，客户端会看到「206 + 500 粘在一起」的损坏响应。
    """
    # 直接测 _reply 的兜底契约：它自己不该炸，且不改动已写出的字节
    src = tmp_path / "recon_e2e_partial.bin"
    src.write_bytes(b"xyz")

    async def _download_info(group, rid):
        return (src.as_posix(), "partial.bin")

    port, svc = await http_server(download_info=_download_info)

    # 只在这个测试里换掉 _reply，且必须用 monkeypatch 记账式恢复。
    #
    # 坑：`_reply` 是 `@staticmethod`。直接写
    #     original = svc.__class__._reply      # 取到的是「裸函数」
    #     ...
    #     svc.__class__._reply = original      # 描述符被拆掉，变普通函数
    # 会把 staticmethod 降级成普通属性，于是 `svc._reply` 变成**绑定方法**
    # （签名从 (writer, code, body) 变成 (code, body)），本测试之后的
    # staged/proxy 分支全部炸成
    #     `_reply() takes 3 positional arguments but 4 were given`
    # —— 这是测试污染，不是产品缺陷。monkeypatch 会按描述符原样还原。
    original = svc.__class__.__dict__["_reply"]  # 拿真正的 staticmethod 描述符

    calls: list[int] = []

    async def _spy(writer, code, body):
        calls.append(code)
        return await original.__func__(writer, code, body)

    monkeypatch.setattr(svc.__class__, "_reply", staticmethod(_spy))

    resp = await _request(port, f"/download?token={TOKEN}&group=g1&id=1")

    # 正常路径：serve_local_file 写完后不再有兜底调用
    assert _head(resp) == "HTTP/1.1 200 OK"
    assert calls == [], f"成功路径不得触发兜底 _reply，实际调用了 {calls}"


# ---------- 端到端：staged 分支 ----------


async def test_endpoint_serves_a_staged_artifact(http_server, tmp_path):
    """staged 分支：写进 `_staged` 的产物必须能经端点取回，未知 token -> 404。"""
    staged_file = tmp_path / "essence.txt"
    staged_file.write_bytes(b"hello-essence")

    port, svc = await http_server()
    svc._staged["tok123"] = {"path": staged_file.as_posix(), "name": "essence.txt"}

    resp = await _request(port, f"/download?token={TOKEN}&staged=tok123")
    assert _head(resp) == "HTTP/1.1 200 OK"
    assert resp.endswith(b"hello-essence")

    assert (
        _head(await _request(port, f"/download?token={TOKEN}&staged=nope"))
        == "HTTP/1.1 404 Not Found"
    )

    # token 已被 pop 语义：staged 是只读一次还是可重复？按实现记录当前行为
    again = await _request(port, f"/download?token={TOKEN}&staged=tok123")
    assert _head(again) in ("HTTP/1.1 200 OK", "HTTP/1.1 404 Not Found")


# ---------- 清理 ----------


@pytest.fixture(autouse=True)
def _no_stray_cache():
    yield
    shutil.rmtree(Path(tempfile.gettempdir()) / "cloudstorage-http", ignore_errors=True)
