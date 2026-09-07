"""PluginConfig 单元测试：默认值 / 显式值 / 类型转换 / 告警 / get 兼容。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import DEFAULTS, PluginConfig  # noqa: E402


def test_empty_config_typed_defaults():
    cfg = PluginConfig({})
    assert cfg.request_interval_ms == 1000
    assert cfg.auto_label is True
    assert cfg.page_size == 10
    assert cfg.download_server_enabled is False
    assert cfg.managed_groups == []
    assert cfg.op_high_priority_kinds == []


def test_explicit_values():
    cfg = PluginConfig({"request_interval_ms": 300, "auto_label": False,
                        "download_token": "t1", "managed_groups": [123]})
    assert cfg.request_interval_ms == 300
    assert cfg.auto_label is False
    assert cfg.download_token == "t1"
    assert cfg.managed_groups == ["123"]


def test_type_coercion():
    cfg = PluginConfig({"request_interval_ms": "1000", "page_size": "15",
                        "auto_scan_interval_hours": "6.5",
                        "download_server_enabled": "true"})
    assert cfg.request_interval_ms == 1000
    assert cfg.page_size == 15
    assert cfg.auto_scan_interval_hours == 6.5
    assert cfg.download_server_enabled is True


def test_coercion_failure_falls_back_to_default():
    cfg = PluginConfig({"request_interval_ms": "abc", "page_size": None})
    assert cfg.request_interval_ms == DEFAULTS["request_interval_ms"]
    assert cfg.page_size == DEFAULTS["page_size"]


def test_get_passthrough_semantics():
    """get() 为 dict 透传：键缺失返回调用点 default，不受 schema 默认值影响。"""
    cfg = PluginConfig({})
    assert cfg.get("request_interval_ms", 500) == 500
    assert cfg.get("missing", None) is None
    cfg2 = PluginConfig({"request_interval_ms": 1000})
    assert cfg2.get("request_interval_ms", 500) == 1000


def test_validate_unknown_key_and_bad_list():
    cfg = PluginConfig({"not_a_key": 1, "managed_groups": "oops"})
    warnings = cfg.validate()
    keys = [w[0] for w in warnings]
    assert "not_a_key" in keys
    assert "managed_groups" in keys


def test_raw_is_independent_copy():
    cfg = PluginConfig({"a": 1})
    raw = cfg.raw
    raw["a"] = 2
    assert cfg.get("a") == 1


def test_nested_wrap_is_idempotent():
    """双重包装不触发序列协议下标访问（回归：曾 KeyError: 0 导致插件加载失败）。"""
    cfg = PluginConfig({"request_interval_ms": 300})
    cfg2 = PluginConfig(cfg)
    assert cfg2.get("request_interval_ms", 500) == 300
    cfg2._data["x"] = 1  # 内部 dict 独立，不污染原实例
    assert cfg.get("x") is None
