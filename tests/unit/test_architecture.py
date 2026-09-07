"""Architecture structure tests — validates the converged single-layer layout.

自 2026-09-05 起 services→application 渐进迁移终止，一次性收敛：
- core/application 是唯一服务层（queue/sync/catalog/files/ingest/bridge + 平铺件）
- core/services 已删除；任何模块不得再引用 core.services / adapters.store
- application 层仅允许经 adapters.external（OpenList 通道，ADR §2）触达适配器
- OneBot 经 ports.onebot_api、持久化经 ports.meta_store、限速经 ports.limiter
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "pages" / "storage-ng"


# ---- Legacy layers must be gone ----

class TestLegacyLayersRemoved:
    """收敛后不得残留旧层目录与兼容壳。"""

    def test_no_core_services_dir(self):
        assert not (ROOT / "core" / "services").exists(), \
            "core/services 已删除，勿再复活（服务一律入 core/application）"

    def test_no_adapters_store_dir(self):
        assert not (ROOT / "adapters" / "store").exists(), \
            "adapters/store 兼容壳已删除，请直接 import adapters.persistence.sqlite"

    @pytest.mark.parametrize("rel", [
        "core/application/tasks.py",
        "core/application/resources.py",
        "core/application/orchestration.py",
        "core/application/database_admin.py",
        "core/application/download.py",
        "core/application/groups.py",
        "core/application/sync.py",
    ])
    def test_no_facade_shims(self, rel):
        assert not (ROOT / rel).exists(), f"兼容壳已删除：{rel}"

    @pytest.mark.parametrize("py_file", [
        *sorted(ROOT.glob("core/**/*.py")),
        *sorted(ROOT.glob("adapters/**/*.py")),
        *sorted(ROOT.glob("commands/**/*.py")),
        *sorted(ROOT.glob("webapi/**/*.py")),
        ROOT / "main.py",
        ROOT / "bootstrap.py",
    ])
    def test_no_legacy_import_paths(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("init file")
        content = py_file.read_text(encoding="utf-8")
        for term in ("core.services", "adapters.store", "application.op_queue"):
            assert term not in content, \
                f"{py_file.relative_to(ROOT)} 引用已删除路径: {term}"


# ---- File size constraints ----

def _file_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


class TestPythonFileSize:
    """Each Python file should be < 700 lines（单层后不再有 legacy 放宽）."""

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("core/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_core_under_700(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("init file")
        lines = _file_lines(py_file)
        assert lines < 700, f"{py_file.relative_to(ROOT)}: {lines} lines >= 700"

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("adapters/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_adapters_under_700(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("init file")
        lines = _file_lines(py_file)
        assert lines < 700, f"{py_file.relative_to(ROOT)}: {lines} lines >= 700"

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("webapi/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_webapi_under_700(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("init file")
        lines = _file_lines(py_file)
        assert lines < 700, f"{py_file.relative_to(ROOT)}: {lines} lines >= 700"


# JS 文件 300 行预算由 node --test structure.test.mjs 门禁覆盖（单一事实源，
# 此处不再重复）。


# ---- Application layer dependency discipline ----

class TestApplicationDependencies:
    """core/application 只允许经 adapters.external 触达适配器（OpenList 通道）。

    OneBot → ports.onebot_api；持久化 → ports.meta_store；限速 → ports.limiter。
    """

    FORBIDDEN_ADAPTERS = (
        "adapters.onebot",
        "adapters.persistence",
        "adapters.limiter",
        "adapters.store",
    )

    @pytest.mark.parametrize(
        "py_file",
        sorted((ROOT / "core" / "application").rglob("*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_no_direct_adapter_imports(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("init file")
        tree = ast.parse(py_file.read_text(encoding="utf-8"),
                         filename=str(py_file))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(alias.name for alias in node.names)
            for mod in mods:
                for bad in self.FORBIDDEN_ADAPTERS:
                    assert not mod.startswith(bad), \
                        f"{py_file.relative_to(ROOT)} import 禁止的适配器: {mod}"


# ---- Ports purity + constants ownership ----

class TestPortsPurity:
    """ports/ 是纯接口层：只依赖标准库/typing，禁止触达 adapters 与 core.application。"""

    FORBIDDEN_PREFIXES = ("adapters.", "core.application")

    @pytest.mark.parametrize(
        "py_file",
        sorted((ROOT / "ports").rglob("*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_ports_no_adapter_or_application_imports(self, py_file):
        tree = ast.parse(py_file.read_text(encoding="utf-8"),
                         filename=str(py_file))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(alias.name for alias in node.names)
            for mod in mods:
                for bad in self.FORBIDDEN_PREFIXES:
                    assert not mod.startswith(bad), \
                        f"{py_file.relative_to(ROOT)} 端口禁止导入: {mod}"


class TestVolumeConstantsSingleDefinition:
    """分卷常量单点定义：只允许 core/application/files/consts.py 定义，消费方经 consts.X 读取。

    读点写在调用处（非 import 绑定），保证测试可对 consts 模块打补丁。
    """

    OWNED_BY = "core.application.files.consts"
    CONSTANTS = ("CHUNK_THRESHOLD_BYTES", "VOLUME_SIZE_BYTES")

    @pytest.mark.parametrize(
        "py_file",
        sorted((ROOT / "core").rglob("*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_no_duplicate_constant_definitions(self, py_file):
        if py_file.relative_to(ROOT).as_posix() == f"{self.OWNED_BY.replace('.', '/')}.py":
            return
        tree = ast.parse(py_file.read_text(encoding="utf-8"),
                         filename=str(py_file))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            names = [t.id for t in targets
                     if isinstance(t, ast.Name) and t.id in self.CONSTANTS]
            assert not names, \
                f"{py_file.relative_to(ROOT)} 重复定义 {names}（唯一归属 {self.OWNED_BY}）"


class TestPortsExplicitExports:
    """显式端口集合：每个 ports 模块的公开 Protocol/实现类必须在 ports.__all__ 导出。"""

    def test_all_port_classes_exported(self):
        import ports

        expected: set[str] = set()
        for py in sorted((ROOT / "ports").glob("*.py")):
            if py.name == "__init__.py":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in tree.body:
                if isinstance(node, (ast.ClassDef,)):
                    if not node.name.startswith("_"):
                        expected.add(node.name)
        exported = set(ports.__all__)
        missing = expected - exported
        assert not missing, f"ports/__init__ 缺少显式导出: {sorted(missing)}"


class TestTestPathsMatchProduction:
    """⑧ 测试路径与生产强一致：tests 中 import 的项目模块必须真实存在。

    防止再次出现「tests import core.services.* 旧路径 + 生产跑 core.application.*」的漂移。
    """

    TOP_PREFIXES = ("core", "adapters", "ports", "webapi", "commands", "pages")

    @pytest.mark.parametrize(
        "py_file",
        sorted((ROOT / "tests").rglob("*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_test_imports_resolve(self, py_file):
        import importlib.util

        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
            for mod in mods:
                if not mod.startswith(self.TOP_PREFIXES):
                    continue
                if mod.startswith("pages."):
                    target = ROOT / Path(*mod.split("."))
                    assert (
                        target.with_suffix(".js").exists()
                        or target.with_suffix(".py").exists()
                        or (target / "__init__.py").exists()
                    ), f"{py_file.relative_to(ROOT)} 导入不存在的模块: {mod}"
                    continue
                assert importlib.util.find_spec(mod) is not None, (
                    f"{py_file.relative_to(ROOT)} 导入不存在的模块: {mod} "
                    f"（测试路径与生产漂移？）"
                )


class TestImportContracts:
    """⑫ import 白名单契约：tools/check_import_contracts.py 的 4 条契约必须全过。"""

    def test_contracts_hold(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "check_import_contracts.py")],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- Route registry consistency ----

class TestRouteRegistry:
    """Route registry should be unique and match webapi imports."""

    def test_routes_file_exists(self):
        routes_file = ROOT / "webapi" / "routes.py"
        assert routes_file.exists()

    def test_routes_are_unique(self):
        routes_file = ROOT / "webapi" / "routes.py"
        content = routes_file.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(routes_file))
        paths = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "ROUTES":
                        if isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Tuple) and len(elt.elts) >= 1:
                                    if isinstance(elt.elts[0], ast.Constant):
                                        paths.append(elt.elts[0].value)
        seen = set()
        for p in paths:
            assert p not in seen, f"Duplicate route path: {p}"
            seen.add(p)


# ---- No removed backend references ----

class TestNoLegacyBackends:
    """Production code must not reference removed database backends."""

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("core/**/*.py")) + sorted(ROOT.glob("adapters/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_no_postgres_refs(self, py_file):
        if py_file.name == "__init__.py":
            pytest.skip("test file")
        if "test_" in py_file.name:
            pytest.skip("test file")
        content = py_file.read_text(encoding="utf-8")
        for term in ("postgres", "opensearch", "psycopg", "scheduler_mode", "storage_mode", "search_mode"):
            assert term not in content.lower(), f"{py_file.relative_to(ROOT)} contains '{term}'"


# ---- core/application layout ----

class TestApplicationLayer:
    """core/application/ 是唯一服务层，关键模块必须存在。"""

    REQUIRED_FILES = [
        "policies.py",
        "transfer.py",
        "distributor.py",
        "netdisk.py",
        "gateway.py",
        "download_server.py",
    ]

    REQUIRED_PACKAGES = [
        "queue",     # op_queue + task_control + op_dispatch + capacity + health
        "sync",      # resource_sync + group_scan
        "catalog",   # resource_query + search_kv + storage_planner
        "files",     # FileOpsService + converter + consts
        "composition",  # 零整治理：spec/integrity/splitter/reassembler
        "ingest",    # CloudIngestService + fetch/essence/video/album
        "bridge",    # BridgeService + submit/inbound/polling/recovery
        "database",  # DatabaseAdminService
    ]

    def test_directory_exists(self):
        assert (ROOT / "core" / "application").is_dir()

    @pytest.mark.parametrize("module", REQUIRED_FILES)
    def test_module_exists(self, module):
        assert (ROOT / "core" / "application" / module).exists(), f"Missing {module}"

    @pytest.mark.parametrize("package", REQUIRED_PACKAGES)
    def test_package_exists(self, package):
        pkg = ROOT / "core" / "application" / package
        assert pkg.is_dir() and (pkg / "__init__.py").exists(), f"Missing {package}/"


# ---- core/runtime/ exists ----

class TestRuntimeLayerExists:
    """core/runtime/ directory must exist."""

    def test_directory_exists(self):
        assert (ROOT / "core" / "runtime").is_dir()

    def test_kernel_exists(self):
        assert (ROOT / "core" / "runtime" / "kernel.py").exists()

    def test_lifecycle_exists(self):
        assert (ROOT / "core" / "runtime" / "lifecycle.py").exists()


# ---- adapters/persistence/sqlite/ exists ----

class TestPersistenceLayerExists:
    """adapters/persistence/sqlite/ directory must exist with key modules."""

    REQUIRED = [
        "connection.py",
        "migrations.py",
        "resources.py",
        "groups.py",
        "volumes.py",
        "folders.py",
        "sync.py",
        "archive.py",
        "search.py",
        "outbox.py",
        "netdisk.py",
        "store.py",
    ]

    def test_directory_exists(self):
        assert (ROOT / "adapters" / "persistence" / "sqlite").is_dir()

    @pytest.mark.parametrize("module", REQUIRED)
    def test_module_exists(self, module):
        assert (ROOT / "adapters" / "persistence" / "sqlite" / module).exists(), \
            f"Missing {module}"


# ---- Frontend modules exist ----

class TestFrontendModules:
    """Active frontend modules must exist (dead shell/router removed 2026-09-07)."""

    ACTIVE_ROOT_FILES = ["main.js", "router.js", "store.js", "store-state.js",
                         "api.js", "constants.js", "icons.js", "index.html"]
    ACTIVE_DIRS = ["views", "components", "features", "utils", "styles"]

    @pytest.mark.parametrize("file", ACTIVE_ROOT_FILES)
    def test_root_module(self, file):
        assert (WEB / file).exists(), f"Missing {file}"

    @pytest.mark.parametrize("dirn", ACTIVE_DIRS)
    def test_directory(self, dirn):
        assert (WEB / dirn).is_dir(), f"Missing {dirn}/"


# ---- Schema version consistency ----

class TestSchemaVersion:
    """Schema version in migrations.py should match expected."""

    def test_schema_version(self):
        migrations = ROOT / "adapters" / "persistence" / "sqlite" / "migrations.py"
        content = migrations.read_text(encoding="utf-8")
        import importlib
        from adapters.persistence.sqlite import migrations as _m
        importlib.reload(_m)
        assert _m.SCHEMA_VERSION >= 17
