"""命令薄壳层测试：权限角色提取（raw_message.sender.role 回退逻辑）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commands.handlers import _role  # noqa: E402


class _FakeMessageObj:
    def __init__(self, raw):
        self.raw_message = raw


class _FakeEvent:
    def __init__(self, raw=None, is_admin=False):
        self.message_obj = _FakeMessageObj(raw or {})
        self._admin = is_admin

    def is_admin(self):
        return self._admin


def test_role_from_raw_sender_owner():
    ev = _FakeEvent({"sender": {"role": "owner"}})
    assert _role(ev) == "admin"


def test_role_from_raw_sender_admin():
    ev = _FakeEvent({"sender": {"role": "admin"}})
    assert _role(ev) == "admin"


def test_role_fallback_member():
    ev = _FakeEvent({"sender": {"role": "member"}})
    assert _role(ev) == "member"


def test_role_fallback_is_admin():
    ev = _FakeEvent({}, is_admin=True)
    assert _role(ev) == "admin"


def test_role_no_raw():
    ev = _FakeEvent(None)
    assert _role(ev) == "member"