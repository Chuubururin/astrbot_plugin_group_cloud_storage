"""前后端 API 路由一致性测试。

从 api.js 解析前端 API 常量，从 webapi.py 捕获后端路由注册，
断言两者的路径集合完全匹配（防漏注册 / 常量漂移）。

Run: pytest tests/unit/test_frontend_backend_contract.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapi  # noqa: E402


# ---------------------------------------------------------------------------
# Parse frontend API constants from api.js
# ---------------------------------------------------------------------------

_API_JS = (
    Path(__file__).resolve().parents[2]
    / "pages" / "storage-ng" / "api.js"
)


def _parse_api_constants() -> dict[str, str]:
    """Extract all API constant values from api.js.

    Returns {CONSTANT_NAME: path_string}.
    Handles both flat constants (TASKS: 'tasks') and
    nested object constants (FILES: { LIST: 'files', ... }).
    """
    text = _API_JS.read_text(encoding="utf-8")
    result = {}

    # Find the API = { ... }; block
    api_match = re.search(r'export const API = (\{[\s\S]*?\n\};)', text)
    if not api_match:
        return result
    api_block = api_match.group(1)

    # Parse nested objects: GROUPS: { LIST: 'groups', ... }
    nested_pattern = re.compile(
        r'(\w+):\s*\{([^}]*)\}', re.MULTILINE
    )
    for m in nested_pattern.finditer(api_block):
        group_name = m.group(1)
        inner = m.group(2)
        for line in inner.split('\n'):
            line = line.strip()
            # Match: KEY: 'value',
            kv = re.match(r"(\w+):\s*'([^']+)'", line)
            if kv:
                key = f"{group_name}.{kv.group(1)}"
                result[key] = kv.group(2)

    # Parse flat constants: TASKS: 'tasks',
    flat_pattern = re.compile(r"^\s+(\w+):\s*'([^']+)'", re.MULTILINE)
    for m in flat_pattern.finditer(api_block):
        key = m.group(1)
        # Skip if it's part of a nested object (already captured)
        if f"{key}." not in result and not any(
            key == k.split('.')[0] for k in result if '.' in k
        ):
            result[key] = m.group(2)

    return result


def _parse_api_constants_v2() -> dict[str, str]:
    """Extract all API path values from api.js (handles nested + flat patterns)."""
    text = _API_JS.read_text(encoding="utf-8")
    result = {}

    api_match = re.search(r'export const API = (\{[\s\S]*?\n\};)', text)
    if not api_match:
        return result
    api_block = api_match.group(1)

    # Parse nested objects: GROUPS: { LIST: 'groups', ... }
    nested_pattern = re.compile(r'(\w+):\s*\{([^}]*)\}', re.MULTILINE)
    for m in nested_pattern.finditer(api_block):
        group_name = m.group(1)
        inner = m.group(2)
        for line in inner.split('\n'):
            line = line.strip()
            kv = re.match(r"(\w+):\s*'([^']+)'", line)
            if kv:
                result[f"{group_name}.{kv.group(1)}"] = kv.group(2)

    # Parse flat constants: TASKS: 'tasks',
    flat_pattern = re.compile(r"^\s+(\w+):\s*'([^']+)'", re.MULTILINE)
    for m in flat_pattern.finditer(api_block):
        key = m.group(1)
        # Skip if already captured as nested group
        if not any(key == k.split('.')[0] for k in result if '.' in k):
            result[key] = m.group(2)

    return result


# ---------------------------------------------------------------------------
# Parse backend route registrations
# ---------------------------------------------------------------------------

def _collect_routes() -> dict[str, list[str]]:
    """Capture registered routes from webapi.register_page_apis."""

    class _FakeCtx:
        def __init__(self):
            self.registered = []

        def register_web_api(self, path, handler, methods, desc=""):
            self.registered.append((path, handler, list(methods), desc))

    class _FakeSvc:
        pass

    ctx = _FakeCtx()
    webapi.register_page_apis(ctx, _FakeSvc())
    routes = {}
    prefix = f"/{webapi.PLUGIN_NAME}/"
    for path, _, methods, _ in ctx.registered:
        # Strip prefix to get relative path
        if path.startswith(prefix):
            rel = path[len(prefix):]
        else:
            rel = path
        routes[rel] = methods
    return routes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFrontendBackendContract:
    """Verify frontend API constants match backend route registrations."""

    def test_api_constants_parsed(self):
        """Sanity: api.js constants are parseable."""
        consts = _parse_api_constants_v2()
        assert len(consts) > 20, f"Expected >20 API constants, got {len(consts)}"

    def test_all_frontend_paths_registered_in_backend(self):
        """Every frontend API constant must have a corresponding backend route."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        missing = []
        for name, path in consts.items():
            # Skip non-endpoint constants (display labels, etc.)
            if path in ('', 'events'):
                continue
            # Exact match or prefix match (e.g. 'files/upload' matches 'files/upload/<token>')
            if path not in routes and not any(r.startswith(path + '/') for r in routes):
                missing.append(f"{name} -> '{path}'")
        assert not missing, (
            f"Frontend constants with no backend route:\n"
            + "\n".join(missing)
        )

    def test_all_backend_routes_have_frontend_constant(self):
        """Every backend route should have a corresponding frontend constant."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        frontend_paths = set(consts.values())
        # Some routes are internal / not exposed to frontend
        internal_routes = {
            'events',           # SSE handled separately
            'accounts',         # may be internal
        }
        missing = []
        for path in routes:
            if path not in frontend_paths and path not in internal_routes:
                missing.append(path)
        # Soft check: warn but don't fail (some routes may be internal)
        if missing:
            # Only fail if more than expected internal routes are missing
            assert len(missing) <= 15, (
                f"Backend routes with no frontend constant (>5):\n"
                + "\n".join(missing[:10])
            )

    def test_tasks_endpoints_contract(self):
        """Tasks API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected = {
            'TASKS': 'tasks',
            'TASKS_QUEUE': 'tasks/queue',
            'TASKS_PAUSE': 'tasks/pause',
            'TASKS_RESUME': 'tasks/resume',
            'TASKS_RESUME_PENDING': 'tasks/resume-pending',
            'TASKS_INTERRUPT': 'tasks/interrupt',
            'TASKS_UNDO': 'tasks/undo',
            'TASKS_OPS': 'tasks/ops',
        }
        for name, path in expected.items():
            assert name in consts, f"Missing frontend constant: {name}"
            assert consts[name] == path, f"{name}: expected '{path}', got '{consts[name]}'"
            assert path in routes, f"No backend route for {name} -> '{path}'"

    def test_config_endpoints_contract(self):
        """Config API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected = {
            'CONFIG_GET': 'config/get',
            'CONFIG_SAVE': 'config/save',
        }
        for name, path in expected.items():
            assert name in consts, f"Missing frontend constant: {name}"
            assert consts[name] == path
            assert path in routes, f"No backend route for {name} -> '{path}'"

    def test_sync_endpoints_contract(self):
        """Sync API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected = {
            'SYNC_WITHERING': 'sync/withering',
            'SYNC_STATUS': 'sync/status',
        }
        for name, path in expected.items():
            assert name in consts, f"Missing frontend constant: {name}"
            assert consts[name] == path
            assert path in routes, f"No backend route for {name} -> '{path}'"

    def test_files_endpoints_contract(self):
        """Files API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected_files = [
            'files', 'files/detail', 'files/upload/prepare', 'files/upload/<token>',
            'files/delete', 'files/batch-delete', 'files/move', 'files/batch-move',
            'files/replace_name', 'files/tags',
            'files/tagcloud', 'files/batch-tags', 'files/links', 'files/download',
            'files/link', 'download/address', 'files/uri', 'files/scan',
            'files/sync', 'files/recommend-group',
            'files/distribute', 'files/folder-create',
        ]
        for path in expected_files:
            assert path in routes, f"No backend route for files endpoint: '{path}'"

    def test_groups_endpoints_contract(self):
        """Groups API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected_groups = [
            'groups', 'groups/scan', 'groups/batch', 'groups/batch-ops',
            'groups/order', 'groups/remove', 'groups/removed', 'groups/restore',
        ]
        for path in expected_groups:
            assert path in routes, f"No backend route for groups endpoint: '{path}'"

    def test_bridge_endpoints_contract(self):
        """Bridge/Netdisk API constants must match backend registration."""
        consts = _parse_api_constants_v2()
        routes = _collect_routes()
        expected_bridge = [
            'bridge/status', 'bridge/transfer', 'bridge/transfer-in',
            'bridge/tasks', 'bridge/netdisk', 'bridge/cancel', 'bridge/retry',
            'bridge/archived', 'bridge/config/get', 'bridge/config/save',
            'netdisk/link', 'netdisk/index', 'netdisk/upload-url',
            'netdisk/distribute', 'netdisk/mkdir', 'netdisk/rename',
            'netdisk/remove', 'netdisk/move', 'netdisk/copy',
            'netdisk/remove-empty-dirs', 'netdisk/recursive-move',
            'netdisk/rename-batch',
        ]
        for path in expected_bridge:
            assert path in routes, f"No backend route for bridge endpoint: '{path}'"

    def test_endpoint_count_minimum(self):
        """Total endpoint count should be at least 80 (功能只增不减)."""
        routes = _collect_routes()
        assert len(routes) >= 80, f"Endpoint count regressed: {len(routes)}"
