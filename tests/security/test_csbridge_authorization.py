"""H3 regression — /csbridge must authorize every action (CWE-862).

handle_csbridge was the only command handler without a permission check,
so any user (including in a private chat) could cancel or retry any bridge
task by id.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commands.handlers import Services, handle_csbridge  # noqa: E402
from core.application.policies import PermissionService  # noqa: E402


class _FakeMessageObj:
    def __init__(self, raw):
        self.raw_message = raw


class _FakeEvent:
    def __init__(self, sender_id="100", group_id="", role="member"):
        self._sender = sender_id
        self._group = group_id
        self._role = role
        self.message_obj = _FakeMessageObj({"sender": {"role": role}})

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def is_admin(self):
        return self._role in ("owner", "admin")


class _FakeBridge:
    def __init__(self):
        self.cancelled: list[str] = []
        self.retried: list[str] = []

    async def status(self, task_id=None):
        return {"state": "pending", "pending_out": 1, "pending_in": 0}

    async def cancel(self, task_id):
        self.cancelled.append(task_id)
        return True

    async def retry(self, task_id):
        self.retried.append(task_id)
        return True


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    async def get_archive_map_by_task(self, task_id):
        return self._rows.get(task_id)


def _services(rows, admins=()):
    return Services(
        permission=PermissionService(global_admin_qqs=list(admins)),
        store=_FakeStore(rows),
        api=None,
        sync=None,
        query=None,
        stats=None,
        bridge=_FakeBridge(),
    )


ROWS = {"t-1": {"task_id": "t-1", "group_id": "111"}, "t-2": {"task_id": "t-2", "group_id": "222"}}


def test_plain_member_cannot_cancel_another_groups_task():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="111", role="member")
    out = asyncio.run(handle_csbridge(ev, s, "cancel", "t-2"))
    assert "权限不足" in out
    assert s.bridge.cancelled == []


def test_group_admin_can_cancel_own_groups_task():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="111", role="admin")
    out = asyncio.run(handle_csbridge(ev, s, "cancel", "t-1"))
    assert "权限不足" not in out
    assert s.bridge.cancelled == ["t-1"]


def test_group_admin_cannot_cancel_another_groups_task():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="111", role="admin")
    out = asyncio.run(handle_csbridge(ev, s, "retry", "t-2"))
    assert "权限不足" in out
    assert s.bridge.retried == []


def test_global_admin_can_cancel_any_task():
    s = _services(ROWS, admins=["900"])
    ev = _FakeEvent(sender_id="900", group_id="", role="member")
    out = asyncio.run(handle_csbridge(ev, s, "cancel", "t-2"))
    assert "权限不足" not in out
    assert s.bridge.cancelled == ["t-2"]


def test_aggregate_status_requires_global_admin():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="111", role="admin")
    out = asyncio.run(handle_csbridge(ev, s, "status", ""))
    assert "权限不足" in out


def test_unknown_task_is_refused():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="111", role="admin")
    out = asyncio.run(handle_csbridge(ev, s, "cancel", "nope"))
    assert "任务不存在" in out
    assert s.bridge.cancelled == []


def test_private_chat_member_cannot_touch_a_task():
    s = _services(ROWS)
    ev = _FakeEvent(sender_id="100", group_id="", role="member")
    out = asyncio.run(handle_csbridge(ev, s, "cancel", "t-1"))
    assert "权限不足" in out
    assert s.bridge.cancelled == []
