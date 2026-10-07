"""回归：ingest 日志不得携带 URL 内嵌凭据。

/csfetch 支持 sftp://user:pass@host、smb://user:pass@host，旧代码把完整
URL 打进 INFO 日志（fetch.py "fetched {url}"）。判据：_log_safe_url 输出
不含 userinfo 与 query。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.ingest.fetch import _log_safe_url  # noqa: E402


def test_strips_sftp_credentials():
    out = _log_safe_url("sftp://alice:s3cret@nas.example.com:2222/files/a.zip")
    assert out == "sftp://nas.example.com:2222/files/a.zip"
    assert "s3cret" not in out and "alice" not in out


def test_strips_smb_credentials_and_query():
    out = _log_safe_url("smb://u:p@host/share/f.bin?token=abc123")
    assert "u:p" not in out and "token=abc123" not in out
    assert out == "smb://host/share/f.bin"


def test_plain_http_url_shape_kept():
    out = _log_safe_url("https://cdn.example.com/a/b.mp4?sig=x")
    assert out == "https://cdn.example.com/a/b.mp4"
