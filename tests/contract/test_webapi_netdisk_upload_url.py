"""api_netdisk_upload_url contract: OpenList-side failures must surface as a
reasoned 502 instead of escaping to the framework's opaque 500.

This was the only write handler in webapi/netdisk_mutation.py without a
try/except — its siblings (mkdir/rename/remove/...) all answer with a logged,
human-readable error. The contract here pins the same shape for the offline
download path, plus the empty-task-list answer (OpenList rejects a URL by
returning no task at all).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import OpenListApiError  # noqa: E402
from webapi.netdisk_mutation import api_netdisk_upload_url  # noqa: E402


def _json_response(data):
    return {"__kind__": "json", "data": data}


def _error_response(msg, status_code=400):
    return {"__kind__": "error", "message": msg, "status_code": status_code}


class _Bridge:
    """Scripted bridge: returns preset tasks or raises a preset exception."""

    def __init__(self, tasks=None, exc=None):
        self.tasks = tasks
        self.exc = exc
        self.calls: list[tuple[list[str], str]] = []

    async def submit_offline_download(self, urls, dst_dir):
        self.calls.append((list(urls), dst_dir))
        if self.exc is not None:
            raise self.exc
        return self.tasks


def _svc(bridge) -> SimpleNamespace:
    # s.ready=None keeps _ensure_ready a no-op (same stance as the sibling
    # handler tests in test_webapi_tasks.py)
    return SimpleNamespace(bridge=bridge, ready=None)


def _payload(monkeypatch, payload: dict):
    async def _json_body():
        return payload

    monkeypatch.setattr("webapi.netdisk_mutation.json_body", _json_body)
    monkeypatch.setattr("webapi.netdisk_mutation.json_response", _json_response)
    monkeypatch.setattr("webapi.netdisk_mutation.error_response", _error_response)


@pytest.mark.asyncio
async def test_upload_url_rejects_non_http_scheme(monkeypatch):
    bridge = _Bridge()
    _payload(monkeypatch, {"url": "ftp://example.com/file.bin"})
    resp = await api_netdisk_upload_url(_svc(bridge))
    assert resp["status_code"] == 400
    assert bridge.calls == [], "must reject before touching the bridge"


@pytest.mark.asyncio
async def test_upload_url_surfaces_openlist_rejection(monkeypatch):
    bridge = _Bridge(exc=OpenListApiError("url task failed", code=500))
    _payload(monkeypatch, {"url": "https://example.com/file.bin", "dir": "/docs"})
    resp = await api_netdisk_upload_url(_svc(bridge))
    assert resp["status_code"] == 502
    assert "url task failed" in resp["message"]
    assert bridge.calls == [(["https://example.com/file.bin"], "/docs")]


@pytest.mark.asyncio
async def test_upload_url_empty_task_list_is_not_ok(monkeypatch):
    bridge = _Bridge(tasks=[])
    _payload(monkeypatch, {"url": "https://example.com/file.bin"})
    resp = await api_netdisk_upload_url(_svc(bridge))
    assert resp["status_code"] == 502
    assert "未返回离线任务" in resp["message"]


@pytest.mark.asyncio
async def test_upload_url_success_carries_task_id(monkeypatch):
    from adapters.external.openlist_dto import OfflineTask

    task = OfflineTask(
        id="t1", name="f.bin", state="pending", status="", progress=0.0, error=""
    )
    bridge = _Bridge(tasks=[task])
    _payload(monkeypatch, {"url": "https://example.com/file.bin"})
    resp = await api_netdisk_upload_url(_svc(bridge))
    assert resp["__kind__"] == "json"
    assert resp["data"]["ok"] is True
    assert resp["data"]["task_id"] == "t1"
