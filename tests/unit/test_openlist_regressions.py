"""OpenList 客户端回归测试：分页死循环保护 + 信封 data: null 兜底。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external import openlist as ol  # noqa: E402
from adapters.external.base import OpenListApiError  # noqa: E402
from adapters.external.openlist import OpenListClient  # noqa: E402


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def _client(request_result=None, side_effect=None):
    """构造已注入 token 的客户端 + 模拟的 httpx 层（返回 patcher）。"""
    client = OpenListClient(base_url="https://example.com:5244", token="t")
    http = AsyncMock()
    if side_effect is not None:
        http.request.side_effect = side_effect
    else:
        http.request.return_value = request_result
    return client, http, patch.object(client, "_ensure_client", AsyncMock(return_value=http))


# ---------- list_dir 分页保护 ----------

@pytest.mark.asyncio
async def test_list_dir_stops_on_empty_page_when_has_more_stuck():
    """服务端持续返回 has_more=true 且空页时不得死循环。"""
    client, http, patcher = _client(_resp({"code": 200, "data": {"content": [], "has_more": True}}))
    with patcher:
        files = await client.list_dir("/g")
    assert files == []
    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_list_dir_caps_pages_when_has_more_never_clears():
    """每页都有内容且 has_more 永不清零：受最大页数保护，不能无限累积。"""
    def _page(method, path, **kwargs):
        n = kwargs["json"]["page"]
        return _resp({
            "code": 200,
            "data": {
                "content": [{"name": f"f{n}", "size": 1, "is_dir": False, "modified": ""}],
                "has_more": True,
            },
        })

    client, http, patcher = _client(side_effect=_page)
    with patcher:
        files = await client.list_dir("/g")
    assert len(files) == ol._MAX_LIST_PAGES
    assert http.request.await_count == ol._MAX_LIST_PAGES


@pytest.mark.asyncio
async def test_list_dir_normal_pagination_unaffected():
    """正常分页（末页 has_more=false）不受保护逻辑影响。"""
    pages = {
        1: {"content": [{"name": "a", "size": 1, "is_dir": False, "modified": ""}], "has_more": True},
        2: {"content": [{"name": "b", "size": 1, "is_dir": False, "modified": ""}], "has_more": False},
    }

    def _page(method, path, **kwargs):
        return _resp({"code": 200, "data": pages[kwargs["json"]["page"]]})

    client, _http, patcher = _client(side_effect=_page)
    with patcher:
        files = await client.list_dir("/g")
    assert [f.name for f in files] == ["a", "b"]


# ---------- 信封 data: null ----------

@pytest.mark.asyncio
async def test_list_dir_page_tolerates_null_data():
    client, _http, patcher = _client(_resp({"code": 200, "data": None}))
    with patcher:
        files, has_more = await client.list_dir_page("/g", 1)
    assert files == [] and has_more is False


@pytest.mark.asyncio
async def test_task_lists_tolerate_null_data():
    client, _http, patcher = _client(_resp({"code": 200, "data": None}))
    with patcher:
        assert await client.tasks_undone() == []
        assert await client.tasks_done() == []


@pytest.mark.asyncio
async def test_submit_offline_download_tolerates_null_data():
    client, _http, patcher = _client(_resp({"code": 200, "data": None}))
    with patcher:
        assert await client.submit_offline_download(["http://x/f.bin"], "/g") == []


@pytest.mark.asyncio
async def test_get_raw_url_raises_domain_error_on_null_data():
    """fs/link 与 fs/get 都返回 data: null → 抛 OpenListApiError，不是 AttributeError。"""
    client, _http, patcher = _client(_resp({"code": 200, "data": None}))
    with patcher:
        with pytest.raises(OpenListApiError):
            await client.get_raw_url("/g/f.bin")


@pytest.mark.asyncio
async def test_login_tolerates_null_data():
    """登录信封 data: null → 缺 token 的领域错误，不是 AttributeError。"""
    client = OpenListClient(
        base_url="https://example.com:5244", username="admin", password="pw"
    )
    http = AsyncMock()
    http.post.return_value = _resp({"code": 200, "data": None})
    with patch.object(client, "_ensure_client", AsyncMock(return_value=http)):
        with pytest.raises(OpenListApiError):
            await client.ensure_token()
