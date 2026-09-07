"""WebAPI handler 集成测试：Tasks 端点（暂停/继续/中断/撤销/操作流/断点续传）。

通过 mock Services + monkeypatch json_body/request，端到端验证 handler 逻辑
（不依赖真实 SQLite / OpQueue，纯 handler 层测试）。

Run: pytest tests/contract/test_webapi_tasks.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapi  # noqa: E402
import webapi.webapi as _wp  # noqa: E402
import core.api_validate as _av  # noqa: E402
from core.api_validate import ApiValidationError  # noqa: E402
from webapi.tasks import (
    api_tasks,
    api_tasks_queue,
    api_tasks_pause,
    api_tasks_resume,
    api_tasks_interrupt,
    api_tasks_undo,
    api_tasks_ops,
    api_tasks_resume_pending,
)
from webapi.sync import api_sync_withering, api_sync_status


# ---------------------------------------------------------------------------
# Mock json_response / error_response to return plain dicts (handler tests
# don't need real Starlette responses)
# ---------------------------------------------------------------------------

def _json_response(data):
    return data

def _error_response(msg, status_code=400):
    return {"status": "error", "message": msg}

@pytest.fixture(autouse=True)
def _patch_responses(monkeypatch):
    monkeypatch.setattr("webapi.tasks.json_response", _json_response)
    monkeypatch.setattr("webapi.tasks.error_response", _error_response)
    monkeypatch.setattr("webapi.sync.json_response", _json_response)
    monkeypatch.setattr("webapi.sync.error_response", _error_response)
    async def _facade_json_body():
        return await webapi.webapi.json_body()
    monkeypatch.setattr("webapi.tasks.json_body", _facade_json_body)
    monkeypatch.setattr("webapi.sync.json_body", _facade_json_body)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal store stub for ops_last_for_resource."""

    def __init__(self):
        self._ops_last = None

    async def ops_last_for_resource(self, kind, rid):
        return self._ops_last


class _FakeTaskControl:
    """TaskControlService stub capturing all calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._pause_result = {"status": "paused", "task_id": ""}
        self._resume_result = {"status": "resumed", "task_id": ""}
        self._interrupt_result = {"status": "interrupted", "task_id": ""}
        self._undo_result = {"status": "undone", "task_id": ""}
        self._tasks = []
        self._ops = []
        self._queue_status = {"depth": 0, "running": 0, "paused_ids": []}
        self._resume_pending_result = {"resumed": 0}

    async def list_tasks(self, **kw):
        self.calls.append(("list_tasks", kw))
        return self._tasks

    async def queue_status(self):
        return self._queue_status

    async def pause(self, task_id):
        self.calls.append(("pause", {"task_id": task_id}))
        return {**self._pause_result, "task_id": task_id}

    async def resume(self, task_id):
        self.calls.append(("resume", {"task_id": task_id}))
        return {**self._resume_result, "task_id": task_id}

    async def interrupt(self, task_id):
        self.calls.append(("interrupt", {"task_id": task_id}))
        return {**self._interrupt_result, "task_id": task_id}

    async def undo(self, task_id=None, group_id=None, resource_id=None):
        self.calls.append(("undo", {"task_id": task_id, "group_id": group_id, "resource_id": resource_id}))
        return {**self._undo_result, "task_id": task_id or ""}

    async def ops(self, task_id):
        self.calls.append(("ops", {"task_id": task_id}))
        return self._ops

    async def on_state(self, task_id, kind, target, payload, state, error=None):
        pass


class _FakeQueue:
    """OpQueue stub for submit/resume_pending."""

    def __init__(self):
        self.submitted: list[tuple[str, dict]] = []
        self._submit_id = "task-001"

    async def submit(self, kind, target="", payload=None):
        self.submitted.append((kind, {"target": target, "payload": payload}))
        return self._submit_id


class _FakeScan:
    async def is_page_managed(self, gid, managed):
        return gid in (managed or [])


def _make_services(**overrides):
    tc = _FakeTaskControl()
    q = _FakeQueue()
    store = _FakeStore()
    scan = _FakeScan()
    svc = SimpleNamespace(
        task_control=tc,
        queue=q,
        store=store,
        scan=scan,
        config={"managed_groups": [], "auto_scan_interval_hours": 6},
        ready=None,
    )
    for k, v in overrides.items():
        setattr(svc, k, v)
    return svc


def _patch_json_body(data):
    """Monkeypatch json_body to return fixed data."""
    async def _fake():
        return data if isinstance(data, dict) else {}
    return _fake


def _patch_json_body_exc(exc):
    async def _raise():
        raise exc
    return _raise


# ---------------------------------------------------------------------------
# T-5: api_tasks (记录查询)
# ---------------------------------------------------------------------------

class TestApiTasks:
    @pytest.mark.asyncio
    async def test_tasks_list_default(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = [{"task_id": "t1", "state": "done"}]
        monkeypatch.setattr(_av, "request", SimpleNamespace(
            query=SimpleNamespace(get=lambda *a, **kw: None),
            json=lambda default={}: {},
        ))
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks(svc)
        assert result["tasks"] == [{"task_id": "t1", "state": "done"}]
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_tasks_with_filters(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"state": "running", "kind": "move_file", "limit": 10, "offset": 5}
        ))
        result = await api_tasks(svc)
        call_kw = svc.task_control.calls[-1][1]
        assert call_kw["state"] == "running"
        assert call_kw["kind"] == "move_file"
        assert call_kw["limit"] == 10
        assert call_kw["offset"] == 5

    @pytest.mark.asyncio
    async def test_tasks_empty(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks(svc)
        assert result["tasks"] == []
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# T-5: api_tasks_queue (队列状态)
# ---------------------------------------------------------------------------

class TestApiTasksQueue:
    @pytest.mark.asyncio
    async def test_queue_status(self):
        svc = _make_services()
        svc.task_control._queue_status = {"depth": 3, "running": 1, "paused_ids": ["p1"]}
        result = await api_tasks_queue(svc)
        assert result["depth"] == 3
        assert result["running"] == 1
        assert "p1" in result["paused_ids"]


# ---------------------------------------------------------------------------
# T-5: api_tasks_pause
# ---------------------------------------------------------------------------

class TestApiTasksPause:
    @pytest.mark.asyncio
    async def test_pause_success(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": "t1"}))
        result = await api_tasks_pause(svc)
        assert result["status"] == "paused"
        assert result["task_id"] == "t1"
        assert ("pause", {"task_id": "t1"}) in svc.task_control.calls

    @pytest.mark.asyncio
    async def test_pause_missing_task_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_pause(svc)
        assert result["status"] == "error"
        assert "task_id" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_pause_empty_task_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": ""}))
        result = await api_tasks_pause(svc)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# T-5: api_tasks_resume
# ---------------------------------------------------------------------------

class TestApiTasksResume:
    @pytest.mark.asyncio
    async def test_resume_success(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": "t2"}))
        result = await api_tasks_resume(svc)
        assert result["status"] == "resumed"
        assert result["task_id"] == "t2"

    @pytest.mark.asyncio
    async def test_resume_missing_task_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_resume(svc)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# T-5: api_tasks_interrupt
# ---------------------------------------------------------------------------

class TestApiTasksInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_success(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": "t3"}))
        result = await api_tasks_interrupt(svc)
        assert result["status"] == "interrupted"
        assert result["task_id"] == "t3"

    @pytest.mark.asyncio
    async def test_interrupt_missing_task_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_interrupt(svc)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# T-5: api_tasks_undo
# ---------------------------------------------------------------------------

class TestApiTasksUndo:
    @pytest.mark.asyncio
    async def test_undo_by_task_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": "t4"}))
        result = await api_tasks_undo(svc)
        assert result["task_id"] == "t4"
        call_kw = [c for c in svc.task_control.calls if c[0] == "undo"][-1][1]
        assert call_kw["task_id"] == "t4"

    @pytest.mark.asyncio
    async def test_undo_no_params(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_undo(svc)
        # Should not crash; undo with no params returns a result dict
        assert "status" in result or "task_id" in result

    @pytest.mark.asyncio
    async def test_undo_by_group_id_and_resource_id(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"group_id": "g1", "id": 42}
        ))
        result = await api_tasks_undo(svc)
        call_kw = [c for c in svc.task_control.calls if c[0] == "undo"][-1][1]
        assert call_kw["group_id"] == "g1"
        assert call_kw["resource_id"] == 42


# ---------------------------------------------------------------------------
# T-5: api_tasks_ops
# ---------------------------------------------------------------------------

class TestApiTasksOps:
    @pytest.mark.asyncio
    async def test_ops_by_task_id(self, monkeypatch):
        svc = _make_services()
        svc.task_control._ops = [{"action": "move", "state": "done"}]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"task_id": "t5"}))
        result = await api_tasks_ops(svc)
        assert result["task_id"] == "t5"
        assert len(result["ops"]) == 1

    @pytest.mark.asyncio
    async def test_ops_by_group_id_and_resource_id(self, monkeypatch):
        svc = _make_services()
        svc.store._ops_last = {"action": "tag", "state": "done"}
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"group_id": "g1", "id": 7}
        ))
        result = await api_tasks_ops(svc)
        assert len(result["ops"]) == 1
        assert result["ops"][0]["action"] == "tag"

    @pytest.mark.asyncio
    async def test_ops_missing_params(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_ops(svc)
        assert result["status"] == "error"
        assert "task_id" in result.get("message", "")


# ---------------------------------------------------------------------------
# D-4: api_tasks_resume_pending (断点续传)
# ---------------------------------------------------------------------------

class TestApiResumePending:
    @pytest.mark.asyncio
    async def test_resume_pending_empty(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = []
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_resume_pending(svc)
        assert result["resumed"] == 0
        assert "无待恢复" in result.get("note", "")

    @pytest.mark.asyncio
    async def test_resume_pending_whitelist(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = [
            {"task_id": "t1", "kind": "convert_volumes", "target": "g1", "payload": "{}"},
            {"task_id": "t2", "kind": "video_upload", "target": "g1", "payload": "{}"},
            {"task_id": "t3", "kind": "netdisk_index", "target": "g1", "payload": "{}"},
            {"task_id": "t4", "kind": "move_file", "target": "g1", "payload": "{}"},
        ]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_resume_pending(svc)
        assert result["resumed"] == 3
        # move_file should be skipped (not in whitelist)
        submitted_kinds = [k for k, _ in svc.queue.submitted]
        assert "move_file" not in submitted_kinds

    @pytest.mark.asyncio
    async def test_resume_pending_invalid_json_payload(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = [
            {"task_id": "t1", "kind": "convert_volumes", "target": "g1", "payload": "not-json"},
        ]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_tasks_resume_pending(svc)
        assert result["resumed"] == 1  # should handle invalid JSON gracefully


# ---------------------------------------------------------------------------
# D-4: api_sync_withering (凋零差分手动触发)
# ---------------------------------------------------------------------------

class TestApiSyncWithering:
    @pytest.mark.asyncio
    async def test_withering_all(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await api_sync_withering(svc)
        assert result["mode"] == "all"
        assert "task_id" in result
        assert svc.queue.submitted[0][0] == "diff_file_scan"

    @pytest.mark.asyncio
    async def test_withering_specific_groups(self, monkeypatch):
        svc = _make_services()
        async def _is_managed(gid, mg):
            return gid in ["g1", "g2"]
        svc.scan = SimpleNamespace(is_page_managed=_is_managed)
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"group_ids": ["g1", "g2", "g3"]}
        ))
        result = await api_sync_withering(svc)
        assert result["groups"] == 2  # g3 not managed

    @pytest.mark.asyncio
    async def test_withering_no_valid_groups(self, monkeypatch):
        svc = _make_services()
        async def _is_managed(gid, mg):
            return False
        svc.scan = SimpleNamespace(is_page_managed=_is_managed)
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"group_ids": ["bad1"]}
        ))
        result = await api_sync_withering(svc)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_withering_invalid_payload(self, monkeypatch):
        svc = _make_services()
        # Invalid payload (list instead of dict) should fallback to all
        async def _bad_json():
            return [1, 2, 3]
        monkeypatch.setattr(webapi.webapi, "json_body", _bad_json)
        # Non-dict payload: (payload or {}).get() would fail, but json_body
        # exception handler catches it and defaults to {}
        result = await api_sync_withering(svc)
        # Should fallback to "all" mode due to payload parsing error
        assert result.get("mode") == "all" or "task_id" in result


# ---------------------------------------------------------------------------
# D-4: api_sync_status
# ---------------------------------------------------------------------------

class TestApiSyncStatus:
    @pytest.mark.asyncio
    async def test_sync_status_default(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = []
        result = await api_sync_status(svc)
        assert "auto_scan_hours" in result
        assert result["auto_scan_hours"] == 6
        assert result["auto_scan_enabled"] is True
        assert result["running_count"] == 0

    @pytest.mark.asyncio
    async def test_sync_status_with_recent_scan(self, monkeypatch):
        svc = _make_services()
        svc.task_control._tasks = [
            {"task_id": "scan1", "state": "done", "created_at": "2026-09-01",
             "updated_at": "2026-09-01", "error": None}
        ]
        result = await api_sync_status(svc)
        assert result["last_diff_scan"]["task_id"] == "scan1"

    @pytest.mark.asyncio
    async def test_sync_status_disabled(self, monkeypatch):
        svc = _make_services()
        svc.config["auto_scan_interval_hours"] = 0
        result = await api_sync_status(svc)
        assert result["auto_scan_enabled"] is False
