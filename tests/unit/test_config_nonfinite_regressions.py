"""非有限数值配置回归（M22 / M23 / request_interval）。

json.load 默认接受 ``Infinity`` / ``1e400``（→ float inf）。修复前：

- M22：PluginConfig 的 int 转换只捕 (TypeError, ValueError)，
  ``int(float('inf'))`` 抛的 OverflowError 直接穿透 —— 文档承诺的
  “转换失败回退默认值”失效；fetch_max_bytes / bridge_*_bytes / page_size
  在 bootstrap 装配期被读取 → Main.__init__ 失败，插件整体加载不起来。
- M23：validate_config() 里 ``int(vtb)`` 同样漏捕 → 配置校验自身抛异常，
  而它在装配前被无条件调用，同样中断启动。
- request_interval：未做有限性检查（与 units.parse_size / parse_duration
  “拒绝非有限值”的项目约定不一致）；inf 被接受后传给限速器，
  ``await asyncio.sleep(inf)`` 永不返回 → 每次 QQ 调用前 acquire 全部挂死。
"""

from __future__ import annotations

import sys
from math import inf, nan
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import DEFAULTS, PluginConfig  # noqa: E402
from core.config.schema import validate_config  # noqa: E402

# bootstrap 装配期会读取的 int 属性（M22 的爆炸半径）
_INT_KEYS = [
    "request_interval_ms",
    "page_size",
    "essence_chunk_size",
    "video_segment_seconds",
    "volume_threshold_mb",
    "fetch_max_bytes",
    "bridge_min_bytes",
    "bridge_max_bytes",
    "fetch_timeout_sec",
    "download_http_port",
    "download_sftp_port",
    "openlist_poll_interval_sec",
]

_NONFINITE = [inf, -inf, nan, 1e400]


@pytest.mark.parametrize("key", _INT_KEYS)
@pytest.mark.parametrize("bad", _NONFINITE)
def test_m22_int_property_falls_back_to_default(key, bad):
    """int 转换失败（含 OverflowError）必须回退 schema 默认值，不抛异常。"""
    assert getattr(PluginConfig({key: bad}), key) == DEFAULTS[key]


def test_m22_legacy_byte_keys_nonfinite():
    """_size_property 的 legacy 分支 ``int(legacy)`` 同样要吞 OverflowError。"""
    assert (
        PluginConfig({"fetch_max_bytes": inf}).fetch_max_bytes
        == DEFAULTS["fetch_max_bytes"]
    )
    assert (
        PluginConfig({"bridge_max_bytes": 1e400}).bridge_max_bytes
        == DEFAULTS["bridge_max_bytes"]
    )
    # volume_threshold_mb 走 legacy_unit="mb"，回退到 volume_threshold 默认 95MB
    assert PluginConfig({"volume_threshold_mb": inf}).volume_threshold_bytes == 95_000_000


def test_m22_size_string_key_still_rejects_nonfinite():
    """字符串单位键的守卫不变：parse_size 拒绝 inf，回落 legacy/default。"""
    cfg = PluginConfig({"fetch_max_size": inf, "fetch_max_bytes": 123})
    assert cfg.fetch_max_bytes == 123


def test_m22_construction_of_poisoned_config_never_raises():
    """json.load 出来的 “毒” 配置整份构造 + 全属性读取不炸。"""
    data = {k: inf for k in _INT_KEYS}
    cfg = PluginConfig(data)
    for key in _INT_KEYS:
        getattr(cfg, key)  # 不应抛任何异常


def test_m23_validate_config_warns_instead_of_raising():
    warnings = validate_config({"volume_threshold_mb": inf})
    hit = [w for w in warnings if w[0] == "volume_threshold_mb"]
    assert hit, "应产出 volume_threshold_mb 告警而不是抛异常"
    assert "期望 int" in hit[0][1]


@pytest.mark.parametrize("bad", _NONFINITE)
def test_m23_validate_config_nonfinite_variants(bad):
    assert any(w[0] == "volume_threshold_mb" for w in validate_config({"volume_threshold_mb": bad}))


def test_m23_validate_config_still_flags_small_threshold():
    """修复不改变既有语义：正常可转换值仍然只按 “过小” 告警。"""
    warnings = validate_config({"volume_threshold_mb": 5})
    hit = [w for w in warnings if w[0] == "volume_threshold_mb"]
    assert hit and "阈值过小" in hit[0][1]
    assert not [w for w in validate_config({"volume_threshold_mb": 95}) if w[0] == "volume_threshold_mb"]


@pytest.mark.parametrize("bad", _NONFINITE)
def test_request_interval_rejects_nonfinite(bad):
    """inf/nan 必须回落到 request_interval_ms/1000，绝不交给限速器。"""
    got = PluginConfig({"request_interval": bad}).request_interval
    assert got == DEFAULTS["request_interval_ms"] / 1000.0
    assert got == 1.0


def test_request_interval_still_honors_finite_values():
    assert PluginConfig({"request_interval": 0.5}).request_interval == 0.5
    assert PluginConfig({"request_interval": "2.5"}).request_interval == 2.5
    # 0 / 负数 / 非数值仍走 legacy ms 回落（既有语义不变）
    assert PluginConfig({"request_interval": 0, "request_interval_ms": 2000}).request_interval == 2.0
    assert PluginConfig({"request_interval": "abc", "request_interval_ms": 300}).request_interval == 0.3
