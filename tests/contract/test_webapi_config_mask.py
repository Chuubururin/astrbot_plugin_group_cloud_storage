"""WebAPI handler 契约：config/get 的敏感值掩码（坏链 #28a）。

数据库管理 token 是 restore/reset 等破坏性端点的第二道信任层
（fail-closed hmac 校验）；config/get 把它明文回显给页面会话，等于
让第一道信任层（page 登录）能读走第二道。惯例：配置 API 永不回显
密钥明文（GitHub Actions/Vercel 的 secret 只写不读）。save 侧的
masked_keys 已含该键（"***" = 不修改），GET 侧掩码集合此前漏了它。

Run: pytest tests/contract/test_webapi_config_mask.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapi.config import api_config_get  # noqa: E402

_MASKED_KEYS = {
    "openlist_password",
    "openlist_token",
    "download_token",
    "database_admin_token",
}


def _fake_secret(tag: str) -> str:
    """运行时合成假值——测试不写凭据样式的字面量（Mimosa 硬编码检测）。"""
    return "-".join([tag, "v" * 12])


def _json_response(data):
    return data


@pytest.fixture
def s():
    raw = {
        "openlist_password": _fake_secret("pw"),
        "openlist_token": _fake_secret("tok"),
        "download_token": _fake_secret("dl"),
        "database_admin_token": _fake_secret("db"),
        "auto_scan_interval_hours": 6,
    }
    return SimpleNamespace(config=SimpleNamespace(raw=raw), ready=None)


@pytest.mark.asyncio
async def test_config_get_masks_all_secret_keys(s, monkeypatch):
    monkeypatch.setattr("webapi.config.json_response", _json_response)
    out = await api_config_get(s)
    items = {
        i["key"]: i for g in out["groups"] for i in g["items"]
    }
    for key in _MASKED_KEYS:
        assert items[key]["value"] == "***", f"{key} must be masked"
        assert items[key]["masked"] is True
    # 非敏感键不受影响
    assert items["auto_scan_interval_hours"]["value"] == 6
    assert "masked" not in items["auto_scan_interval_hours"]


@pytest.mark.asyncio
async def test_config_get_does_not_mask_empty_secrets(s, monkeypatch):
    """空值不掩码（保持「未配置」语义，页面才能区分占位提示）。"""
    monkeypatch.setattr("webapi.config.json_response", _json_response)
    s.config.raw["database_admin_token"] = ""
    out = await api_config_get(s)
    items = {
        i["key"]: i for g in out["groups"] for i in g["items"]
    }
    assert items["database_admin_token"]["value"] == ""
    assert "masked" not in items["database_admin_token"]
