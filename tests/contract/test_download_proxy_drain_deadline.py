"""L6 regression: the proxy body writes must not hang on a stalled client.

Background
----------
A client that opens a connection and never reads (TCP zero window) makes
`StreamWriter.drain()` block until the kernel send buffer drains -- which
never happens. Both streaming helpers therefore need a deadline:

  - download_server_io.py: `_drain()` with `_DRAIN_TIMEOUT`
  - download_proxy.py:     `_drain()` with `_DRAIN_TIMEOUT`  (this file)

The proxy path used bare `await writer.drain()` until F-3 routed every
single-file download through it. On a stall the coroutine never returned,
so the upstream CDN response was never closed (`resp.aclose()` sits after
the loop) -- a leaked connection per stalled client.

These tests pin the *deadline behaviour itself*, independent of which
helper is being exercised, by driving a writer whose drain() never
completes.
"""

import asyncio
import pathlib

import pytest

import core.application.download_proxy as dpx


class _HangingWriter:
    """A writer whose drain() never completes: the zero-window client.

    `write` is a no-op (we only care that drain is awaited with a bound),
    and drain blocks forever so the deadline is the only way out.
    """

    def __init__(self):
        self.written = 0
        self.closed = False

    def write(self, b):
        self.written += len(b)

    async def drain(self):
        await asyncio.Event().wait()  # never set

    def close(self):
        self.closed = True


def test_drain_helper_has_a_deadline(monkeypatch):
    """`_drain()` must raise TimeoutError instead of hanging forever."""
    monkeypatch.setattr(dpx, "_DRAIN_TIMEOUT", 0.05)
    writer = _HangingWriter()

    async def _go():
        with pytest.raises(asyncio.TimeoutError):
            await dpx._drain(writer)

    # Outer bound so a regression fails rather than wedging the suite.
    asyncio.run(asyncio.wait_for(_go(), timeout=5.0))


def test_proxy_module_defines_a_drain_timeout():
    """The constant must exist and be finite/positive (mirrors the io module)."""
    assert hasattr(dpx, "_DRAIN_TIMEOUT"), "download_proxy lost its drain deadline"
    assert isinstance(dpx._DRAIN_TIMEOUT, (int, float))
    assert dpx._DRAIN_TIMEOUT > 0


def test_no_bare_writer_drain_left_in_body_loops():
    """Both body loops must go through `_drain`, not raw `writer.drain()`.

    Guards the regression at the source level so a future edit cannot
    silently reintroduce the unbounded await.
    """
    src = pathlib.Path(dpx.__file__).read_text(encoding="utf-8")
    assert "await writer.drain()" not in src, (
        "bare writer.drain() reintroduced -- use _drain(writer)"
    )
    # And the helper must actually be used at both streaming sites.
    assert src.count("await _drain(writer)") == 2, src.count("await _drain(writer)")


@pytest.mark.asyncio
async def test_serve_staged_times_out_on_a_stalled_client(monkeypatch, tmp_path):
    """serve_staged must propagate the deadline, not hang the request.

    Wrapped in an outer `wait_for` so that if the deadline ever regresses
    this test FAILS instead of hanging forever -- a test that wedges the
    suite is worse than no test at all (reverse-validated: with the bare
    `writer.drain()` restored, the unwrapped form hung indefinitely).
    """
    monkeypatch.setattr(dpx, "_DRAIN_TIMEOUT", 0.05)
    src = tmp_path / "staged.bin"
    src.write_bytes(b"X" * (dpx._STREAM_CHUNK * 3))  # several write/drain rounds
    writer = _HangingWriter()

    async def _reply(w, code, body):
        return None

    async def _go():
        with pytest.raises(asyncio.TimeoutError):
            await dpx.serve_staged(
                writer, {"path": str(src), "name": "a.bin"}, _reply
            )

    # Outer bound: comfortably above _DRAIN_TIMEOUT, far below "forever".
    await asyncio.wait_for(_go(), timeout=5.0)
