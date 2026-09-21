"""W-3a 契约测试：OpenListClient 构造失败必须 fail-open。

根因
----
``bootstrap.build_components`` 原先**无防护**地构造 ``OpenListClient``：

    if cfg.openlist_enabled and cfg.openlist_base_url:
        openlist_client = OpenListClient(...)   # 可能抛 ExternalApiError

``OpenListClient.__init__`` 会跑 ``validate_base_url``（scheme / host / DNS
三层 SSRF 校验）。任一拒绝都会穿透 ``build_components`` → ``_init_runtime``
→ ``Main.__init__``，AstrBot 构造插件失败 ⇒ **8 个命令 handler 与 aiocqhttp
事件钩子全部消失**。这是网盘侧的配置错误对无关命令的连带伤害。

契约
----
1. 构造失败不得外抛 —— ``build_components`` 必须正常返回。
2. 降级必须精确：``openlist_client`` / ``bridge`` / ``netdisk`` 三者同时为 None。
3. 降级必须响亮：error 级日志里带上原始异常文本与肇事配置值。
4. 其余 15 个组件键必须完整 —— 即 8 个命令依赖的服务装配照常。
5. 健康配置的行为**不变**（不能为了容错而吞掉正常路径）。

注意本文件刻意用**真实**的 ``OpenListClient`` + **真实**的非法 URL（形如
``ftp://...``），而不是 mock 掉构造器：只有真跑一遍 ``validate_base_url``
才能证明「拒绝」这一分支确实被 ``except`` 捕获。用 mock 抛异常会绕过
真正的失败点，等于测了个假。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Mock astrbot.api before importing bootstrap（模块级导入安全）
astrbot_mock = MagicMock()
sys.modules.setdefault("astrbot", MagicMock())
sys.modules.setdefault("astrbot.api", astrbot_mock)
sys.modules.setdefault("astrbot.api.logger", astrbot_mock.logger)

from bootstrap import build_components  # noqa: E402
from core.config import PluginConfig  # noqa: E402

EXPECTED_KEYS = {
    "store", "api", "perm", "sync", "limiter", "queue", "scan", "ops",
    "transfer", "ingest", "dlserver", "gateway", "bridge", "openlist_client",
    "task_control", "services", "auto_scan_hours", "database_admin",
}

HEALTHY_URL = "http://openlist:5244"


def _fake_handlers():
    async def fake_bind(action, **params):
        return {}

    async def fake_handler(op):
        pass

    async def fake_ready():
        pass

    return fake_bind, fake_handler, fake_ready


def _build(tmp_path, **config_overrides):
    """Run the real build_components with a controlled config."""
    bind, handler, ready = _fake_handlers()
    config = {
        "openlist_enabled": True,
        "openlist_base_url": HEALTHY_URL,
        # Allow the private address, otherwise the healthy case itself would be
        # rejected by SSRF and the test could not tell a fix from a no-op.
        "openlist_allow_private_address": True,
    }
    config.update(config_overrides)
    return build_components(
        bind_call_action=bind,
        run_handler=handler,
        ready=ready,
        config=config,
        data_dir=tmp_path,
    )


# ---------- 基线：健康配置行为不变 ----------


def test_healthy_config_still_wires_every_component(tmp_path):
    """先钉住正常路径，证明容错没有顺手吞掉功能。"""
    comps = _build(tmp_path)
    assert set(comps) == EXPECTED_KEYS
    assert comps["openlist_client"] is not None
    assert comps["bridge"] is not None
    # netdisk 不经顶层字典暴露，只挂在 Services 门面上
    assert comps["services"].netdisk is not None


def test_healthy_bridge_shares_the_client_instance(tmp_path):
    """bridge / netdisk 必须复用同一个 client（否则 token 状态会分裂）。"""
    comps = _build(tmp_path)
    assert comps["services"].bridge is comps["bridge"]
    assert comps["services"].bridge._client is comps["openlist_client"]
    assert comps["services"].netdisk._client is comps["openlist_client"]


# ---------- 核心：构造失败必须 fail-open ----------


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://openlist:5244",  # scheme 不在白名单
        "file:///etc/passwd",  # 非 http(s)
        "http://",  # 无 hostname
    ],
)
def test_build_survives_an_invalid_base_url(tmp_path, bad_url):
    """非法 base_url 不得让 build_components 抛异常。"""
    comps = _build(tmp_path, openlist_base_url=bad_url)
    assert set(comps) == EXPECTED_KEYS


def test_invalid_base_url_leaves_all_three_netdisk_components_none(tmp_path):
    """降级粒度：client / bridge / netdisk 三者一起 None。

    只把 client 置 None 会留下持有 None client 的 bridge —— 那是在**运行期**
    才炸，比构造期炸更难查。
    """
    comps = _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    assert comps["openlist_client"] is None
    assert comps["bridge"] is None
    assert comps["services"].bridge is None
    assert comps["services"].netdisk is None


def test_invalid_base_url_does_not_cost_unrelated_handlers(tmp_path):
    """W-3a 的**唯一目的**：网盘坏了不能拖垮无关命令。

    这些服务与 OpenList 无关，必须照常装配。
    """
    comps = _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    for key in (
        "store", "api", "sync", "queue", "scan", "ops",
        "transfer", "ingest", "dlserver", "gateway", "task_control",
    ):
        assert comps[key] is not None, f"{key} 被网盘配置错误连带摧毁"


def test_invalid_base_url_still_builds_the_services_facade(tmp_path):
    """Services 门面必须可用，否则 handler 连取 store 都做不到。"""
    comps = _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    assert comps["services"] is not None
    assert isinstance(comps["services"].config, PluginConfig)
    assert comps["services"].store is comps["store"]
    assert comps["services"].queue is comps["queue"]


# ---------- 响亮失败：日志必须指出肇事值与原因 ----------


def test_construction_failure_is_logged_at_error_level(tmp_path, caplog):
    comps = _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    assert comps["openlist_client"] is None
    errors = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "OpenList" in r.getMessage()
    ]
    assert errors, "构造失败必须留下 error 级日志"
    text = errors[0].getMessage()
    assert "continues without" in text


def test_error_log_carries_the_offending_value(tmp_path, caplog):
    """日志里要有肇事 URL —— 否则用户只看到「失败了」不知道该改什么。"""
    _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "ftp://openlist:5244" in joined


def test_error_log_carries_the_underlying_reason(tmp_path, caplog):
    """原始异常文本必须透出（scheme 不被允许），不能只写「初始化失败」。"""
    _build(tmp_path, openlist_base_url="ftp://openlist:5244")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "scheme" in joined.lower()


# ---------- 边界 ----------


def test_disabled_openlist_skips_construction_entirely(tmp_path, caplog):
    """openlist_enabled=false 是**正常**关闭，不是错误 —— 不该刷 error 日志。"""
    comps = _build(tmp_path, openlist_enabled=False)
    assert comps["openlist_client"] is None
    assert comps["bridge"] is None
    assert comps["services"].netdisk is None
    assert set(comps) == EXPECTED_KEYS
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, "显式关闭网盘不应产生 error 日志"


def test_empty_base_url_skips_construction(tmp_path, caplog):
    """空 base_url 与 disabled 同义：跳过，不报错。"""
    comps = _build(tmp_path, openlist_base_url="")
    assert comps["openlist_client"] is None
    assert comps["bridge"] is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors


def test_ssrf_rejection_of_a_private_literal_ip_is_tolerated(tmp_path, caplog):
    """allow_private=False + 私有字面 IP → SSRF 拒绝 → 也走 fail-open。

    这条与 scheme 拒绝走的是 ``validate_base_url`` 的**不同分支**
    （``_check_ip_address``，非 ``_check_dns``），覆盖「校验器里任何一条拒绝
    都不得致命」。刻意用字面 IP：无需 DNS，结果确定，不会因测试机 hosts
    差异而跳过。
    """
    comps = _build(
        tmp_path,
        openlist_base_url="http://10.0.0.1:5244",
        openlist_allow_private_address=False,
    )
    assert set(comps) == EXPECTED_KEYS
    assert comps["openlist_client"] is None
    assert comps["bridge"] is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "restricted address" in joined or "10.0.0.1" in joined
