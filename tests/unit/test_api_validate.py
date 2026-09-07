"""api_validate 单元测试：pick / qi / json_body / ApiValidationError。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.api_validate as av  # noqa: E402
from core.api_validate import (ApiValidationError, json_body, pick,  # noqa: E402
                               qi)


# ---------- pick ----------

def test_pick_cast_and_default():
    assert pick({"a": 1}, "a", cast=int) == 1
    assert pick({}, "a", cast=int, default=5) == 5
    assert pick({"a": "10"}, "a", cast=int) == 10


def test_pick_required_missing():
    with pytest.raises(ApiValidationError) as e:
        pick({}, "gid", required=True)
    assert "gid" in str(e.value) and "缺少" in str(e.value)


def test_pick_required_none_value():
    with pytest.raises(ApiValidationError):
        pick({"gid": None}, "gid", required=True)


def test_pick_cast_failure_message():
    with pytest.raises(ApiValidationError) as e:
        pick({"id": "abc"}, "id", cast=int)
    assert "id" in str(e.value) and "int" in str(e.value)


def test_pick_list_type():
    with pytest.raises(ApiValidationError):
        pick({"ids": "1,2"}, "ids", cast=list)
    assert pick({"ids": [1, 2]}, "ids", cast=list) == [1, 2]


def test_pick_enum():
    assert pick({"mode": "all"}, "mode", enum=("all", "range")) == "all"
    with pytest.raises(ApiValidationError) as e:
        pick({"mode": "x"}, "mode", enum=("all", "range"))
    assert "all|range" in str(e.value)


def test_pick_empty_not_allowed():
    with pytest.raises(ApiValidationError):
        pick({"name": ""}, "name", empty_allowed=False)
    assert pick({"name": ""}, "name", empty_allowed=True) == ""


def test_pick_bool_strings():
    assert pick({"v": "true"}, "v", cast=bool) is True
    assert pick({"v": "0"}, "v", cast=bool) is False


# ---------- qi ----------

def test_qi_valid_and_default():
    assert qi("42") == 42
    assert qi("") == 0
    assert qi(None, default=7) == 7


def test_qi_invalid_raises_with_field():
    with pytest.raises(ApiValidationError) as e:
        qi("abc", field="id")
    assert "id" in str(e.value) and "整数" in str(e.value)


# ---------- json_body ----------

class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self, default=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.asyncio
async def test_json_body_dict_passthrough(monkeypatch):
    monkeypatch.setattr(av, "request", _FakeRequest({"a": 1}))
    assert await json_body() == {"a": 1}


@pytest.mark.asyncio
async def test_json_body_list_rejected(monkeypatch):
    monkeypatch.setattr(av, "request", _FakeRequest([1, 2]))
    with pytest.raises(ApiValidationError) as e:
        await json_body()
    assert "JSON 对象" in str(e.value)


@pytest.mark.asyncio
async def test_json_body_parse_error_falls_back_to_empty(monkeypatch):
    monkeypatch.setattr(av, "request", _FakeRequest(RuntimeError("bad json")))
    assert await json_body() == {}
