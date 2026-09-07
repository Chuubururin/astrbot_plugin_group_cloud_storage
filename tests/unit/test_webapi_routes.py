"""Page 路由注册清单测试（HL-22 端点级契约：任务记录与控制 v15 端点入表）。

以桩 Context 捕获 register_web_api 注册项，断言：
- 新增 7 个任务端点存在且方法与契约一致（HL-04 三处同步的代码侧）
- 端点总数基线（63 → 70）不倒退（功能只增不减）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapi  # noqa: E402


class _FakeContext:
    def __init__(self):
        self.registered: list[tuple[str, object, list, str]] = []

    def register_web_api(self, path, handler, methods, desc=""):
        self.registered.append((path, handler, list(methods), desc))


class _FakeServices:
    """注册期不触碰服务属性（_Bound 惰性绑定）。"""


def _collect() -> dict[str, list[str]]:
    ctx = _FakeContext()
    webapi.register_page_apis(ctx, _FakeServices())
    out: dict[str, list[str]] = {}
    for path, _, methods, _ in ctx.registered:
        out[path] = methods
    return out


def test_task_control_routes_present():
    routes = _collect()
    prefix = f"/{webapi.PLUGIN_NAME}"
    # tasks 为 GET/POST 双通道（2026-08-31：前端 apiPost 通道；契约只增不减）
    assert f"{prefix}/tasks" in routes and routes[f"{prefix}/tasks"] == ["GET", "POST"]
    assert f"{prefix}/tasks/queue" in routes and routes[f"{prefix}/tasks/queue"] == ["GET"]
    assert f"{prefix}/tasks/pause" in routes and routes[f"{prefix}/tasks/pause"] == ["POST"]
    assert f"{prefix}/tasks/resume" in routes and routes[f"{prefix}/tasks/resume"] == ["POST"]
    assert f"{prefix}/tasks/interrupt" in routes and routes[f"{prefix}/tasks/interrupt"] == ["POST"]
    assert f"{prefix}/tasks/undo" in routes and routes[f"{prefix}/tasks/undo"] == ["POST"]
    assert f"{prefix}/tasks/ops" in routes and routes[f"{prefix}/tasks/ops"] == ["POST"]


def test_config_routes_present():
    """配置中心端点登记（D-7）。"""
    routes = _collect()
    prefix = f"/{webapi.PLUGIN_NAME}"
    assert f"{prefix}/config/get" in routes and routes[f"{prefix}/config/get"] == ["GET"]
    assert f"{prefix}/config/save" in routes and routes[f"{prefix}/config/save"] == ["POST"]


def test_recommend_group_route_present():
    """推荐上传群端点登记（N-07，2026-09-01）。"""
    routes = _collect()
    prefix = f"/{webapi.PLUGIN_NAME}"
    assert f"{prefix}/files/recommend-group" in routes
    assert routes[f"{prefix}/files/recommend-group"] == ["GET"]


def test_distribute_routes_present():
    """下载分发端点登记（W2-A，2026-09-02）。"""
    routes = _collect()
    prefix = f"/{webapi.PLUGIN_NAME}"
    for r in ("files/distribute", "albums/distribute", "essence/distribute", "netdisk/distribute"):
        assert f"{prefix}/{r}" in routes
        assert routes[f"{prefix}/{r}"] == ["POST"]


def test_endpoint_count_not_less_than_baseline():
    """功能只增不减：端点总数 ≥ 既有基线 63。"""
    routes = _collect()
    assert len(routes) >= 76, f"endpoint count regressed: {len(routes)}"

def test_aggregate_capacity_defaults():
    """2026-09-03 元数据准确性：聚合（已用/总容量/群数）——
    used 缺失→本地索引；cap 缺失→10GB/群兜底。"""
    import webapi

    class _G:
        def __init__(self, gid, used, total):
            self.group_id = gid
            self.used_space = used
            self.total_space = total

    groups = [_G("a", 0, 0), _G("b", 100, 20 * 1024 ** 3), _G("c", 50, 0)]
    used, cap, n = webapi._aggregate_capacity(groups, {"a": 77})
    assert n == 3
    assert used == 77 + 100 + 50
    assert cap == 10 * 1024 ** 3 + 20 * 1024 ** 3 + 10 * 1024 ** 3
    assert webapi.GROUP_TOTAL_DEFAULT == 10 * 1024 ** 3
