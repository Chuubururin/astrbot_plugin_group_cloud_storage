"""Issue #8 — RFC 6266 Content-Disposition must carry a *usable* filename.

Root cause this file pins shut
------------------------------
Every download endpoint used to emit only the RFC 5987 parameter::

    Content-Disposition: attachment; filename*=UTF-8''%E6%8E%A2%E9%92%88.png

``filename*=`` is understood by modern clients, but the legacy ``filename=``
parameter is what older clients (and a fair number of HTTP libraries and
shell tools) actually look at. The two failing shapes were:

1. ``filename=`` was **omitted entirely** on the proxy / staged / local-file
   streams (``download_proxy.py``, ``download_server_io.py``), so a client
   that ignores ``filename*=`` had no name at all;
2. ``filename=`` was **hard-coded to "download"** in
   ``webapi/resources_mutation.py``, which dropped both the real stem and
   the extension — the user saved an extension-less ``download`` file.

The contract now: every one of these responses carries
``filename="<ascii>"; filename*=UTF-8''<percent-encoded-utf8>``, the ASCII
fallback preserves the extension, and the fallback is only the literal
``"download"`` when there is genuinely no usable name left.
"""

import re
from pathlib import Path

import pytest

import core.application.download_proxy as dpx
import core.application.download_server_io as dio
import webapi.resources_mutation as res

CRLF = "\r\n"

# ---------------------------------------------------------------- helpers


def _head_only(payload: bytes) -> str:
    """Decode just the status/header block of a raw HTTP response."""
    return payload.split(b"\r\n\r\n", 1)[0].decode("latin-1")


def _disp_from(payload: bytes) -> str:
    """Extract the raw Content-Disposition value from a full response."""
    for line in _head_only(payload).split(CRLF):
        if line.lower().startswith("content-disposition:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no Content-Disposition in: {_head_only(payload)}")


def _ascii_param(raw: str) -> str:
    """Return the legacy RFC6266 ``filename=`` value (not the starred one)."""
    assert "filename*=UTF-8''" in raw, f"no RFC5987 parameter at all: {raw}"
    m = re.search(r'(?<!\*)filename="([^"]*)"', raw)
    assert m is not None, f"missing legacy filename= parameter: {raw}"
    return m.group(1)


def _star_param(raw: str) -> str:
    """Return the percent-encoded RFC5987 ``filename*=`` value."""
    m = re.search(r"filename\*=UTF-8''(\S+)", raw)
    assert m is not None, f"missing RFC5987 filename* parameter: {raw}"
    return m.group(1)


class _Sink:
    """Minimal asyncio.StreamWriter stand-in (write + drain only)."""

    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    async def drain(self):
        return None


async def _reply(writer, code, body):
    writer.write(f"HTTP/1.1 {code}{CRLF}".encode() + body)


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


# ------------------------------------------------- 1. helper-level contract


@pytest.mark.parametrize(
    "name,expect",
    [
        ("probe_\u63a2\u9488.png", "probe___.png"),  # 2 CJK -> 2x "_", ext kept
        ("\u62a5\u544a 2026.pdf", "__ 2026.pdf"),  # 2 CJK; space (0x20) kept
        ("plain.txt", "plain.txt"),  # pure ASCII untouched
        ('a"b.png', "a_b.png"),  # quote sanitised
        ("no-ext", "no-ext"),  # nothing to keep
        ("", "download"),  # empty -> fallback only
    ],
)
def test_ascii_fallback_keeps_extension_and_is_ascii(name, expect):
    """The RFC6266 fallback must stay ASCII *and* preserve the extension.

    The old behaviour was a flat ``"download"``; every one of the non-empty
    cases here would have lost the extension, which is the user-visible
    defect from Issue #8.
    """
    got = dpx.ascii_fallback(name)
    assert got == expect, (name, got)
    assert got.isascii(), f"non-ascii leaked into filename=: {got!r}"
    assert '"' not in got, f"quote would break the header: {got!r}"
    assert "/" not in got and "\\" not in got, f"path separator in name: {got!r}"


def test_ascii_fallback_never_returns_empty():
    """A blank name falls back to 'download'; a name whose every char is
    non-ASCII becomes underscores — but never an empty string.

    An empty ``filename=""`` is worse than useless: some clients then derive
    the name from the URL tail, which is exactly the spec-segment bug this
    whole fix chain exists to kill.
    """
    assert dpx.ascii_fallback("") == "download"
    assert dpx.ascii_fallback("   ") == "download"
    # Ideographic space is not stripped, it is simply non-ASCII -> "_".
    assert dpx.ascii_fallback("\u3000") == "_"
    # The invariant that actually matters, for every degenerate input:
    for name in ("", "   ", "\u3000", "\u63a2\u9488", "\\", "//"):
        got = dpx.ascii_fallback(name)
        assert got, f"empty fallback for {name!r}"
        assert got.isascii(), (name, got)


def test_content_disposition_carries_both_parameters():
    """Both RFC6266 (legacy) and RFC5987 (extended) parameters are present."""
    raw = dpx._content_disposition("probe_\u63a2\u9488.png")
    assert raw.startswith("attachment; "), raw
    assert _ascii_param(raw) == "probe___.png", raw
    assert _star_param(raw) == "probe_%E6%8E%A2%E9%92%88.png", raw
    assert _ascii_param(raw) != "download", raw


def test_content_disposition_is_latin1_encodable():
    """The head is written with ``.encode('latin-1')`` — the value must fit.

    A non-ASCII byte in the legacy ``filename=`` would raise
    UnicodeEncodeError *at response time*, turning a cosmetic bug into a
    hard download failure.
    """
    raw = dpx._content_disposition("\u63a2\u9488_\u62a5\u544a_2026.pdf")
    raw.encode("latin-1")  # must not raise
    assert raw.isascii(), f"latin-1 ok but value is not ascii: {raw!r}"


# ------------------------------------------ 2. the three streaming helpers


@pytest.mark.asyncio
async def test_serve_staged_emits_dual_disposition(tmp_path):
    """serve_staged streams a local staged file — both params must show up."""
    src = tmp_path / "staged.bin"
    src.write_bytes(b"PAYLOAD")
    sink = _Sink()
    await dpx.serve_staged(
        sink, {"path": str(src), "name": "\u63a2\u9488_\u62a5\u544a.pdf"}, _reply
    )
    disp = _disp_from(sink.data)
    assert _ascii_param(disp) == "_____.pdf", disp
    assert _star_param(disp) == (
        "%E6%8E%A2%E9%92%88_%E6%8A%A5%E5%91%8A.pdf"
    ), disp
    assert sink.data.endswith(b"PAYLOAD")


@pytest.mark.asyncio
async def test_serve_proxy_emits_dual_disposition(monkeypatch):
    """serve_proxy streams a redirected remote URL — both params must show up.

    Reuses a scripted fake client so the SSRF guard still runs unmocked
    against literal public IPs (as in tests/contract/test_download_proxy.py).
    """
    direct = "https://1.2.3.4/pic/0"
    final = "http://1.2.3.4/pic/800"
    client = _FakeClient(
        [
            _FakeResp(302, {"location": final}),
            _FakeResp(200, {"content-type": "image/png"}, chunks=(b"PNG",)),
        ]
    )
    monkeypatch.setattr(dpx, "_open_client", lambda: client)
    sink = _Sink()
    await dpx.serve_proxy(sink, {"url": direct, "name": "\u63a2\u9488.png"}, _reply)
    disp = _disp_from(sink.data)
    assert _ascii_param(disp) == "__.png", disp
    assert _star_param(disp) == "%E6%8E%A2%E9%92%88.png", disp


@pytest.mark.asyncio
async def test_serve_local_file_emits_dual_disposition(tmp_path):
    """serve_local_file (the local-path branch) — both params, and on both the
    200 and the 206 branch (they share one head builder).

    The source is deliberately *not* named ``recon_*``: ``serve_local_file``
    unlinks ``recon_*`` artifacts in its ``finally`` (they are one-shot
    reassembly output), so a ``recon_``-prefixed source would be gone after
    the first branch and the second would explode with FileNotFoundError.
    """
    src = tmp_path / "vol_part_aa.bin"
    src.write_bytes(b"0123456789")
    cases = (("", b"HTTP/1.1 200 OK"), ("bytes=0-3", b"HTTP/1.1 206"))
    for range_header, expect_code in cases:
        sink = _Sink()
        await dio.serve_local_file(sink, src, "\u62a5\u544a_2026.pdf", range_header)
        assert expect_code in sink.data, (range_header, _head_only(sink.data))
        disp = _disp_from(sink.data)
        assert _ascii_param(disp) == "___2026.pdf", disp
        assert _star_param(disp) == "%E6%8A%A5%E5%91%8A_2026.pdf", disp


@pytest.mark.asyncio
async def test_serve_local_file_reclaims_recon_artifact(tmp_path):
    """The recon_* one-shot cleanup still fires *and* the disposition is right.

    Pins the interaction the previous test has to dodge: cleanup_recon()
    unlinks recon_* sources after streaming, so the dual-parameter header
    must be produced before the file disappears.
    """
    src = tmp_path / "recon_deadbeef.bin"
    src.write_bytes(b"0123456789")
    sink = _Sink()
    await dio.serve_local_file(sink, src, "\u62a5\u544a.pdf", "")
    assert not src.exists(), "recon_* artifact was not reclaimed"
    disp = _disp_from(sink.data)
    assert _ascii_param(disp) == "__.pdf", disp
    assert _star_param(disp) == "%E6%8A%A5%E5%91%8A.pdf", disp


# ------------------------------------------- 3. the WebAPI FileResponse path


def test_resources_mutation_has_no_hardcoded_download_filename():
    """The literal ``filename="download"`` must be gone from the source.

    It is the exact line Issue #8 calls out: it dropped both the stem and
    the extension for every client that ignores ``filename*=``.
    """
    src = Path(res.__file__).read_text(encoding="utf-8")
    assert 'filename="download"' not in src, "hard-coded fallback still present"


def test_resources_mutation_derives_ascii_fallback_from_real_name():
    """The replacement must be a real transformation of the name, not a
    second constant — assert the regex-based derivation is in place."""
    src = Path(res.__file__).read_text(encoding="utf-8")
    assert "ascii_file = re.sub(" in src, "ASCII fallback derivation missing"
    assert "import re" in src, "re not imported"
