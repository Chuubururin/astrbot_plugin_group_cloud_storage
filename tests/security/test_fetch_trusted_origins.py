"""H2 regression — the fetch SSRF gate must allow our OWN download endpoints.

distributor.py feeds dlserver.download_url() (a loopback direct link) into
submit_fetch. With fetch_allow_private_address=False (the default) the SSRF
gate rejected it, so every cloud-to-cloud distribution path failed.

The fix is an allow-list of the endpoints we own (OWASP SSRF Case 1), not a
global relaxation: every other private address stays blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import (  # noqa: E402
    ExternalApiError,
    assert_fetch_host_allowed,
    assert_fetch_url_allowed,
    resolve_and_pin_ip,
)
from core.application.transfer import (  # noqa: E402
    TransferService,
    download_endpoint_origins,
)
from core.config.defaults import DEFAULTS  # noqa: E402

LOOPBACK_URL = "http://127.0.0.1:6186/download?group=1&id=2&token=t"
TRUSTED = frozenset({("127.0.0.1", 6186)})


def _cfg(**over) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(over)
    return cfg


# ---------- the gate itself ----------

def test_loopback_is_still_rejected_without_an_allow_list():
    with pytest.raises(ExternalApiError):
        resolve_and_pin_ip(LOOPBACK_URL, allow_private=False)
    with pytest.raises(ExternalApiError):
        assert_fetch_url_allowed(LOOPBACK_URL, allow_private=False)


def test_allow_listed_origin_passes_the_gate():
    url, host = resolve_and_pin_ip(
        LOOPBACK_URL, allow_private=False, trusted_origins=TRUSTED
    )
    assert url == LOOPBACK_URL
    assert host is None
    assert assert_fetch_url_allowed(
        LOOPBACK_URL, allow_private=False, trusted_origins=TRUSTED
    ) == LOOPBACK_URL


def test_allow_list_is_exact_host_and_port():
    """Same host, different port -> still rejected (no blanket loopback pass)."""
    with pytest.raises(ExternalApiError):
        resolve_and_pin_ip(
            "http://127.0.0.1:9999/download", allow_private=False, trusted_origins=TRUSTED
        )


def test_bare_host_check_honours_the_allow_list():
    assert assert_fetch_host_allowed(
        "127.0.0.1", allow_private=False, port=6186, scheme="http", trusted_origins=TRUSTED
    ) == "127.0.0.1"
    with pytest.raises(ExternalApiError):
        assert_fetch_host_allowed(
            "127.0.0.1", allow_private=False, port=22, scheme="sftp", trusted_origins=TRUSTED
        )


# ---------- endpoint discovery ----------

def test_origins_empty_when_the_download_server_is_disabled():
    assert download_endpoint_origins(_cfg(download_server_enabled=False)) == set()


def test_origins_cover_every_configured_channel():
    origins = download_endpoint_origins(
        _cfg(
            download_server_enabled=True,
            download_server_host="127.0.0.1",
            download_http_port=6186,
            download_sftp_port=6187,
            download_smb_port=0,
        )
    )
    assert origins == {("127.0.0.1", 6186), ("127.0.0.1", 6187)}


def test_wildcard_bind_also_allow_lists_loopback():
    origins = download_endpoint_origins(
        _cfg(
            download_server_enabled=True,
            download_server_host="0.0.0.0",
            download_http_port=6186,
        )
    )
    assert ("127.0.0.1", 6186) in origins
    assert ("::1", 6186) in origins


# ---------- end to end ----------

def test_transfer_service_fetches_its_own_endpoint_but_not_other_private_hosts(tmp_path):
    svc = TransferService(
        MagicMock(), MagicMock(), tmp_path, config=_cfg(), trusted_origins=TRUSTED
    )
    http = svc._adapters["http"]
    assert http._resolve_url(LOOPBACK_URL) == (LOOPBACK_URL, None)
    with pytest.raises(ValueError):
        http._resolve_url("http://192.168.31.108:6186/download")


def test_bootstrap_allow_lists_the_configured_download_endpoint(tmp_path):
    """End-to-end: the wiring that distributor.py depends on."""
    from bootstrap import build_components

    comps = build_components(
        bind_call_action=lambda *a, **k: None,
        run_handler=lambda op: None,
        ready=lambda: None,
        config=_cfg(
            download_server_enabled=True,
            download_server_host="127.0.0.1",
            download_http_port=6186,
            download_token="secret",
        ),
        data_dir=tmp_path,
    )
    transfer = comps["transfer"]
    assert ("127.0.0.1", 6186) in transfer._trusted_origins
    url = "http://127.0.0.1:6186/download?group=1&id=2&token=secret"
    assert transfer._adapters["http"]._resolve_url(url) == (url, None)
