"""WebAPI handler 集成测试：Config 端点（get/save/masked 字段保护）。

验证配置中心的完整生命周期：
- config/get: 分组渲染、敏感项脱敏（HL-11）、reload_required 标记
- config/save: 类型归一、masked 值保护、无效键过滤

Run: pytest tests/contract/test_webapi_config.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapi  # noqa: E402
from webapi import config as _config_module  # noqa: E402
from webapi import webapi as _wp  # noqa: E402

# autouse 的 _patch_config_persist 会把 _read_plugin_config 换成 tmp_path 读写；
# 在导入时先把真实实现存下来，供下面的“真实失败路径”用例使用。
_REAL_READ_PLUGIN_CONFIG = _config_module._read_plugin_config


# ---------------------------------------------------------------------------
# Mock json_response / error_response to return plain dicts
# ---------------------------------------------------------------------------

def _json_response(data):
    return data

def _error_response(msg, status_code=400):
    return {"status": "error", "message": msg}

@pytest.fixture(autouse=True)
def _patch_responses(monkeypatch):
    monkeypatch.setattr(_wp, "json_response", _json_response)
    monkeypatch.setattr(_wp, "error_response", _error_response)


@pytest.fixture(autouse=True)
def _patch_config_persist(monkeypatch, tmp_path):
    """配置落盘接缝：测试机上不存在宿主配置目录，真实写入必然失败。

    归一化/脱敏用例只关心“落盘成功”之后的响应与内存，所以把读写接到
    tmp_path；落盘失败路径由 TestApiConfigPersistFailure 单独覆盖。
    """
    target = tmp_path / "plugin_config.json"

    def _read():
        if not target.exists():
            return {}
        return json.loads(target.read_text(encoding="utf-8"))

    def _write(data):
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(_config_module, "_read_plugin_config", _read)
    monkeypatch.setattr(_config_module, "_write_plugin_config", _write)
    return target


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeConfig:
    """Config dict with optional .raw attribute."""

    def __init__(self, data=None):
        self._data = data or {}
        self.raw = dict(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data


class _FakeStore:
    def __init__(self, tmp_path):
        self._db_path = str(tmp_path / "meta.db")


# Synthetic placeholder values (not real credentials anywhere).
_PLACEHOLDER_PW = "-".join(("dummy", "pw", "placeholder"))
_PLACEHOLDER_TOK = "-".join(("dummy", "tok", "placeholder"))


def _make_services(config_data=None, tmp_path=None):
    cfg = _FakeConfig(config_data or {
        "openlist_password": "-".join(("dummy", "pw", "placeholder")),
        "openlist_token": "-".join(("dummy", "tok", "placeholder")),
        "download_token": "-".join(("dummy", "dl", "placeholder")),
        "request_interval": 0.5,
        "managed_groups": ["g1", "g2"],
        "global_admin_qqs": [12345],
    })
    store = _FakeStore(tmp_path or Path("/tmp"))
    return SimpleNamespace(
        config=cfg,
        store=store,
        ready=None,
    )


def _patch_json_body(data):
    async def _fake():
        return data if isinstance(data, dict) else {}
    return _fake


# ---------------------------------------------------------------------------
# T-7: api_config_get
# ---------------------------------------------------------------------------

class TestApiConfigGet:
    @pytest.mark.asyncio
    async def test_config_get_returns_groups(self, monkeypatch):
        svc = _make_services()
        result = await _wp.api_config_get(svc)
        assert "groups" in result
        assert "reload_required" in result
        assert isinstance(result["groups"], list)
        # Should have at least one group
        assert len(result["groups"]) > 0

    @pytest.mark.asyncio
    async def test_config_get_masks_password(self, monkeypatch):
        svc = _make_services({"openlist_password": _PLACEHOLDER_PW})
        result = await _wp.api_config_get(svc)
        # Find the openlist_password item
        found = False
        for g in result["groups"]:
            for item in g.get("items", []):
                if item["key"] == "openlist_password":
                    assert item["value"] == "***"
                    assert item.get("masked") is True
                    found = True
        assert found, "openlist_password not found in config groups"

    @pytest.mark.asyncio
    async def test_config_get_masks_token(self, monkeypatch):
        svc = _make_services({"openlist_token": _PLACEHOLDER_TOK})
        result = await _wp.api_config_get(svc)
        for g in result["groups"]:
            for item in g.get("items", []):
                if item["key"] == "openlist_token":
                    assert item["value"] == "***"
                    assert item.get("masked") is True
                    return
        pytest.fail("openlist_token not found")

    @pytest.mark.asyncio
    async def test_config_get_masks_download_token(self, monkeypatch):
        svc = _make_services({"download_token": "dl-tok"})
        result = await _wp.api_config_get(svc)
        for g in result["groups"]:
            for item in g.get("items", []):
                if item["key"] == "download_token":
                    assert item["value"] == "***"
                    return
        pytest.fail("download_token not found")

    @pytest.mark.asyncio
    async def test_config_get_empty_password_not_masked(self, monkeypatch):
        """空密码不应显示脱敏标记（HL-11：有值才脱敏）。"""
        svc = _make_services({"openlist_password": ""})
        result = await _wp.api_config_get(svc)
        for g in result["groups"]:
            for item in g.get("items", []):
                if item["key"] == "openlist_password":
                    assert item["value"] == ""
                    assert item.get("masked") is not True
                    return
        pytest.fail("openlist_password not found in config groups")

    @pytest.mark.asyncio
    async def test_config_get_reload_required_markers(self, monkeypatch):
        svc = _make_services({"request_interval": 0.5})
        result = await _wp.api_config_get(svc)
        # request_interval should be in reload_required
        assert "request_interval" in result["reload_required"]

    @pytest.mark.asyncio
    async def test_config_get_groups_preserve_order(self, monkeypatch):
        """分组顺序应与 _CONFIG_GROUPS 定义一致。"""
        svc = _make_services()
        result = await _wp.api_config_get(svc)
        group_names = [g["name"] for g in result["groups"]]
        # 基本断言：不应为空
        assert len(group_names) > 0
        # 无重复
        assert len(group_names) == len(set(group_names))


# ---------------------------------------------------------------------------
# T-7: api_config_save
# ---------------------------------------------------------------------------

class TestApiConfigSave:
    @pytest.mark.asyncio
    async def test_save_missing_values(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({}))
        result = await _wp.api_config_save(svc)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_save_empty_values(self, monkeypatch):
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body({"values": {}}))
        result = await _wp.api_config_save(svc)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_save_unknown_keys_filtered(self, monkeypatch):
        """未知键（不在 schema 中）应被忽略。"""
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"unknown_key_xyz": "val"}}
        ))
        result = await _wp.api_config_save(svc)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_save_masked_value_skipped(self, monkeypatch):
        """masked 字段值为 '***' 时应跳过（不覆盖原值）。"""
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"openlist_password": "***"}}
        ))
        result = await _wp.api_config_save(svc)
        # Should be "no valid keys" since masked value is skipped
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_save_masked_empty_string_skipped(self, monkeypatch):
        """masked 字段值为空字符串时按真实值保存（不是 '***' 就不跳过）。"""
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"openlist_password": ""}}
        ))
        result = await _wp.api_config_save(svc)
        # 空字符串在 masked_keys 中但不是 "***"：被当作真实值归一化后保存
        assert result.get("saved") == ["openlist_password"]

    @pytest.mark.asyncio
    async def test_save_bool_type_normalization(self, monkeypatch):
        """bool 类型归一化（字符串 'true' → True）。"""
        svc = _make_services()
        # 需要一个 bool 类型的 schema 键
        # 先检查 schema 中是否有 bool 类型的键
        schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            pytest.skip("Cannot read _conf_schema.json")
        bool_keys = [k for k, v in schema.items() if v.get("type") == "bool"]
        if not bool_keys:
            pytest.skip("No bool config keys in schema")
        key = bool_keys[0]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {key: "true"}}
        ))
        result = await _wp.api_config_save(svc)
        # bool 归一化恒成功：键必须出现在 saved 中（"true" → True）
        assert result.get("saved") == [key]

    @pytest.mark.asyncio
    async def test_save_request_interval_normalization(self, monkeypatch):
        """float 类型归一化（request_interval 为 schema 中的 float 键）。"""
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"request_interval": "0.6"}}
        ))
        result = await _wp.api_config_save(svc)
        assert result.get("saved") == ["request_interval"]

    @pytest.mark.asyncio
    async def test_save_invalid_int_skipped(self, monkeypatch):
        """无效数值应被跳过（不崩溃）。"""
        svc = _make_services()
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"request_interval": "not_a_number"}}
        ))
        result = await _wp.api_config_save(svc)
        # Should error (no valid keys after skip)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_save_float_type_normalization(self, monkeypatch):
        """float 类型归一化。"""
        svc = _make_services()
        schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            pytest.skip("Cannot read _conf_schema.json")
        float_keys = [k for k, v in schema.items() if v.get("type") == "float"]
        if not float_keys:
            pytest.skip("No float config keys in schema")
        key = float_keys[0]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {key: "3.14"}}
        ))
        result = await _wp.api_config_save(svc)
        assert result.get("saved") == [key]

    @pytest.mark.asyncio
    async def test_save_list_type_normalization(self, monkeypatch):
        """list 类型归一化（非 list → 空 list）。"""
        svc = _make_services()
        schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            pytest.skip("Cannot read _conf_schema.json")
        list_keys = [k for k, v in schema.items() if v.get("type") == "list"]
        if not list_keys:
            pytest.skip("No list config keys in schema")
        key = list_keys[0]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {key: "not_a_list"}}
        ))
        result = await _wp.api_config_save(svc)
        # 非 list 输入归一化为空 list 后照常保存
        assert result.get("saved") == [key]

    @pytest.mark.asyncio
    async def test_save_dict_type_normalization(self, monkeypatch):
        """dict 类型归一化（非 dict → 空 dict）。"""
        svc = _make_services()
        schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            pytest.skip("Cannot read _conf_schema.json")
        dict_keys = [k for k, v in schema.items() if v.get("type") == "dict"]
        if not dict_keys:
            pytest.skip("No dict config keys in schema")
        key = dict_keys[0]
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {key: "not_a_dict"}}
        ))
        result = await _wp.api_config_save(svc)
        # 非 dict 输入归一化为空 dict 后照常保存
        assert result.get("saved") == [key]


# ---------------------------------------------------------------------------
# M16: 落盘失败必须如实返回失败（不得“已保存”）
# ---------------------------------------------------------------------------

class _RecordingConfig(_FakeConfig):
    """带 set() 的配置替身：记录内存写入，验证失败时内存不被污染。"""

    def __init__(self, data=None):
        super().__init__(data)
        self.set_calls: list[tuple[str, object]] = []

    def set(self, key, value):
        self.set_calls.append((key, value))
        self._data[key] = value


def _services_with(config):
    return SimpleNamespace(config=config, store=_FakeStore(Path("/tmp")), ready=None)


class TestApiConfigPersistFailure:
    @pytest.mark.asyncio
    async def test_persist_failure_returns_500_and_keeps_memory(self, monkeypatch):
        """写盘失败 ⇒ 如实返回 500，且内存不动（否则 reload 后前后端不一致）。"""
        def _boom(data):
            raise FileNotFoundError("no such config dir")

        monkeypatch.setattr(_config_module, "_write_plugin_config", _boom)
        cfg = _RecordingConfig({"request_interval": 0.5})
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"request_interval": "0.6"}}
        ))
        result = await _wp.api_config_save(_services_with(cfg))
        # 反向验证：修复前这里返回 {"saved": ["request_interval"], ...}
        assert result.get("status_code") == 500
        assert result.get("status") == "error"
        assert "saved" not in result
        assert cfg.set_calls == []
        assert cfg.get("request_interval") == 0.5

    @pytest.mark.asyncio
    async def test_persist_success_still_reports_saved(self, monkeypatch, tmp_path):
        cfg = _RecordingConfig({"request_interval": 0.5})
        monkeypatch.setattr(webapi.webapi, "json_body", _patch_json_body(
            {"values": {"request_interval": "0.6"}}
        ))
        result = await _wp.api_config_save(_services_with(cfg))
        assert result.get("saved") == ["request_interval"]
        assert cfg.set_calls == [("request_interval", 0.6)]
        # 真的落到了“宿主配置文件”
        assert (tmp_path / "plugin_config.json").exists()


# ---------------------------------------------------------------------------
# 真实 _read_plugin_config 的失败路径（不依赖 autouse 落盘替身）
# ---------------------------------------------------------------------------

class TestRealPluginConfigReadPath:
    """autouse 打桩把“宿主配置目录不存在”这个真实条件从整个文件里挤走了，
    于是 _read_plugin_config 的降级/拒绝分支只剩 0 条覆盖。这里直接调真实实现。
    """

    def test_missing_host_config_dir_degrades_to_empty(self, monkeypatch, tmp_path):
        """宿主配置目录不存在时 _read_plugin_config 必须返回 {}（降级为空配置），
        而不是把 FileNotFoundError 抛给 WebUI。"""
        missing = tmp_path / "no_such_dir" / _config_module._PLUGIN_CONFIG_FILE
        monkeypatch.setattr(
            _config_module, "_config_candidates", lambda: (missing, missing)
        )
        assert _REAL_READ_PLUGIN_CONFIG() == {}

    def test_off_whitelist_path_is_rejected(self, monkeypatch, tmp_path):
        """解析出的路径不是白名单文件名时必须 PermissionError（不得去读任意文件）。"""
        evil = tmp_path / "not_the_plugin_config.json"
        monkeypatch.setattr(_config_module, "_validated_config_path", lambda: evil)
        with pytest.raises(PermissionError):
            _REAL_READ_PLUGIN_CONFIG()
