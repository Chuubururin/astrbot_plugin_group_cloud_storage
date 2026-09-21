"""serve_proxy contract: redirect-following with per-hop SSRF validation.

2026-09-12 real-machine break: QQ CDN album links 302 to the same host
over http with a spec-segment tail; the single-hop proxy answered
"proxy redirect blocked" and the OpenList offline download stored
nothing, dead-ending the album->netdisk chain.
"""

import pytest

import core.application.download_proxy as dpx


class _FakeResp:
    def __init__(self, status, headers, chunks=()):
        self.status_code = status
        self.headers = headers
        self._chunks = list(chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    async def aclose(self):
        return None

    async def aiter_bytes(self, n):
        for c in self._chunks:
            yield c


class _FakeClient:
    """Scripted responses in order; records every requested URL."""

    def __init__(self, script):
        self.script = list(script)
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url, headers=None):
        return (method, url, headers or {})

    async def send(self, req, stream=True):
        self.requested.append(req[1])
        return self.script.pop(0)


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    async def drain(self):
        return None


async def _reply(writer, code, body):
    writer.write(f"HTTP/1.1 {code}\r\n".encode() + body)


# Literal public IPs: no DNS in the tests, and the real SSRF helpers pass
# them through, so the security layer runs unmocked.
_DIRECT = "https://1.2.3.4/pic/0"
_REDIRECT = "http://1.2.3.4/pic/800"


@pytest.mark.asyncio
async def test_serve_proxy_follows_qq_cdn_redirect(monkeypatch):
    """302 to the same CDN over http must be followed; the final stream
    carries the registered real name (the whole point of the proxy)."""
    client = _FakeClient([
        _FakeResp(302, {"location": _REDIRECT}),
        _FakeResp(200, {"content-type": "image/png"}, chunks=(b"PNG", b"DATA")),
    ])
    monkeypatch.setattr(dpx, "_open_client", lambda: client)
    sink = _Sink()
    await dpx.serve_proxy(
        sink, {"url": _DIRECT, "name": "probe_探针.png"}, _reply
    )
    assert client.requested == [_DIRECT, _REDIRECT]
    assert b"HTTP/1.1 200 OK" in sink.data
    assert b"Content-Type: image/png" in sink.data
    assert "filename*=UTF-8''probe_%E6%8E%A2%E9%92%88.png".encode() in sink.data
    assert sink.data.endswith(b"PNGDATA")


@pytest.mark.asyncio
async def test_serve_proxy_blocks_private_redirect(monkeypatch):
    """A redirect into the loopback range must fail the download (502),
    never stream intranet bytes out to the offline-download client."""
    client = _FakeClient([
        _FakeResp(302, {"location": "http://127.0.0.1:9/x"}),
    ])
    monkeypatch.setattr(dpx, "_open_client", lambda: client)
    sink = _Sink()
    await dpx.serve_proxy(sink, {"url": _DIRECT, "name": "probe.png"}, _reply)
    assert b"HTTP/1.1 502" in sink.data
    assert client.requested == [_DIRECT]
