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
        content = py_file.read_text(encoding="utf-8")
        for term in ("core.services", "adapters.store", "application.op_queue"):
            assert term not in content, \
                f"{py_file.relative_to(ROOT)} 引用已删除路径: {term}"


# ---- Dead code must stay deleted ----

class TestDeadCodeStaysDeleted:
    """已裁决删除的死代码不得复活（零调用方 / 指向不存在的方法）。"""

    DEAD_MODULES = [
        # StorageGateway：全仓零方法调用、零属性读取；且 egress()/probe_target()
        # 调用的 TransferService.submit_egress/.probe_target 在仓库中根本不存在。
        "core/application/gateway.py",
    ]

    @pytest.mark.parametrize("rel", DEAD_MODULES)
    def test_module_stays_deleted(self, rel):
        assert not (ROOT / rel).exists(), f"死代码已删除，勿再复活：{rel}"

    def test_database_admin_reset_stays_deleted(self):
        tree = ast.parse(
            (ROOT / "core" / "application" / "database" / "service.py").read_text(encoding="utf-8")
        )
        classes = sorted(c.name for c in ast.walk(tree) if isinstance(c, ast.ClassDef))
        # 反空断言：类名一旦改动，下面的方法扫描会抽到空集而静默通过。
        assert "DatabaseAdminService" in classes, (
            f"未在 service.py 找到 DatabaseAdminService（AST 形状已变？实际类：{classes}）"
        )
        methods = [
            node.name
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef) and cls.name == "DatabaseAdminService"
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert "reset" not in methods, (
            "DatabaseAdminService.reset() 零调用方且为破坏性操作，已删除，勿再复活"
        )


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
        lines = _file_lines(py_file)
        assert lines < 700, f"{py_file.relative_to(ROOT)}: {lines} lines >= 700"

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("adapters/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_adapters_under_700(self, py_file):
        lines = _file_lines(py_file)
        assert lines < 700, f"{py_file.relative_to(ROOT)}: {lines} lines >= 700"

    @pytest.mark.parametrize(
        "py_file",
        sorted(ROOT.glob("webapi/**/*.py")),
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_webapi_under_700(self, py_file):
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


class TestDocDrift:
    """⑤ 文档/schema/路由/前端 API 漂移：tools/check_doc_drift.py 必须全过。

    这是本仓"最大的维护风险"（文档、配置 schema、路由表、前端 API 常量四者互相漂移）
    的机检口径；脚本在 CI 的 contract-checks job 里也会单独跑一次。
    """

    def test_no_drift(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "check_doc_drift.py"), str(ROOT)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_db_auth_gate_is_not_vacuous(self):
        """R4 的正反两面都要成立，否则"全过"没有意义。

        判据是"handler 有没有调用 `_admin`"。只断言 db 路由都调用它，在判据
        恒真（比如把 `called` 算成了整个模块、或 Route 的 auth 位读错）时也会
        通过，所以必须同时断言若干 page 路由被判据认定为未走 `_admin`。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_doc_drift", ROOT / "tools" / "check_doc_drift.py"
        )
        drift = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drift)

        routes = drift.routes_table(ROOT)
        called = drift.handler_calls(ROOT)
        gate = drift.DB_AUTH_ENTRY_POINTS

        db_routes = [r for r in routes if r[3] == "db"]
        assert db_routes, "ROUTES 里已无 db 路由，R4 会变成空转"
        for suffix, _m, handler, _a in db_routes:
            assert not gate.isdisjoint(called.get(handler, set())), (
                f"{suffix} 的 handler {handler} 绕过了 db 鉴权入口"
            )

        # 对照组：page 路由不经 `_admin`，判据必须把它们判为"没走"。
        page = [r for r in routes if r[3] == "page"]
        bypassing = [h for _s, _m, h, _a in page if gate.isdisjoint(called.get(h, set()))]
        assert len(bypassing) >= 10, (
            f"仅 {len(bypassing)} 条 page 路由被判为未走 _admin —— 判据可能恒真"
        )

        # auth 位确实来自 Route 的第 5 个实参，而不是恒为某个定值。
        levels = {r[3] for r in routes}
        assert levels <= {"page", "db", "none"}, levels
        assert len(levels) > 1, "全部路由 auth 同值 —— 第 5 个实参可能读错了"


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
                                if (
                                    isinstance(elt, ast.Call)
                                    and isinstance(elt.func, ast.Name)
                                    and elt.func.id == "Route"
                                    and elt.args
                                    and isinstance(elt.args[0], ast.Constant)
                                ):
                                    paths.append(elt.args[0].value)
        # 反空断言：ROUTES 的 AST 形状若再变，本用例必须红灯，而不是静默空转。
        assert paths, "未能从 ROUTES 抽取任何路径（AST 形状已变？）"
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
        "distribution",  # 分发纯逻辑件：media_spec + text_render
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
        import importlib
        from adapters.persistence.sqlite import migrations as _m
        importlib.reload(_m)
        # 钉死当前版本：升级 schema 时必须同步更新此断言，
        # 防止版本号被误降级或迁移链断裂也静默通过。
        # 29: logical_key 列 + 部分唯一索引 idx_res_logical（一个逻辑文件一行）。
        # 30: DROP v17-25 预建但代码从未落地的僵尸表
        #     （scan_claims/fts_dirty_queue/fts_state/outbox_events）。
        assert _m.SCHEMA_VERSION == 30


# ---- Anti-fragmentation ratchet (P0c) ----

class TestAntiFragmentation:
    """行数门禁的对冲规则：堵住"为压行数而拆出 svc-taking 游离函数 /
    伸手进协作者私有成员"这两类伪模块化增量。

    <700 行门禁只测规模，不测内聚。历史上它已催生 download_server* 四件套
    （函数以 svc 为首参、反向读写原类私有属性）与 distributor 对 bridge
    私有成员的直接访问。本规则用基线白名单**棘轮**：存量违例逐文件冻结，
    新增即红灯；重构消除违例后必须同步调低基线（低于基线也红，防止基线烂掉）。
    """

    # file -> 允许的违例数（只降不升）
    SVC_FN_BASELINE = {
        "core/application/download_cache.py": 2,
        "core/application/download_server_io.py": 6,
        "core/application/download_smb.py": 2,
    }
    CROSS_PRIVATE_BASELINE: dict[str, int] = {
        # P1b 清零：distributor 不再伸手进 bridge 私有成员（改走
        # BridgeService.submit_offline / get_raw_url 公开面）。
    }
    GETATTR_PRIVATE_BASELINE = {
        "core/application/queue/op_dispatch.py": 1,
    }

    @staticmethod
    def _iter_core_py():
        for py in sorted((ROOT / "core").rglob("*.py")):
            if "__pycache__" not in py.parts:
                yield py

    @staticmethod
    def _svc_fn_violations(tree: ast.Module) -> int:
        """模块级函数以 svc 为首参且访问 svc._x（隐式 self 的游离方法）。"""
        n = 0
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in node.args.posonlyargs + node.args.args]
            if not args or args[0] != "svc":
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr.startswith("_")
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "svc"
                ):
                    n += 1
                    break
        return n

    @staticmethod
    def _cross_private_violations(tree: ast.Module) -> int:
        """self.<collab>._private —— 伸手进协作者的私有成员。"""
        n = 0
        for sub in ast.walk(tree):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr.startswith("_")
                and isinstance(sub.value, ast.Attribute)
                and not sub.value.attr.startswith("_")
                and isinstance(sub.value.value, ast.Name)
                and sub.value.value.id == "self"
            ):
                n += 1
        return n

    @staticmethod
    def _getattr_private_violations(tree: ast.Module) -> int:
        """getattr(<他对象>, "_literal") —— 属性名字符串化的私有访问，
        常为迁就 test double 而污染生产代码形状。"""
        n = 0
        for sub in ast.walk(tree):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "getattr"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and isinstance(sub.args[1].value, str)
                and sub.args[1].value.startswith("_")
            ):
                target = sub.args[0]
                if isinstance(target, ast.Name) and target.id in ("self", "svc"):
                    continue
                if isinstance(target, ast.Attribute) and target.attr.startswith("_"):
                    continue
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    continue  # self._own._inner 属自身内部，另议
                n += 1
        return n

    @pytest.mark.parametrize("rule_name,getter,baseline", [
        ("svc-taking 函数访问私有属性", _svc_fn_violations.__func__, SVC_FN_BASELINE),
        ("跨对象私有属性访问", _cross_private_violations.__func__, CROSS_PRIVATE_BASELINE),
        ("getattr 私有字符串访问", _getattr_private_violations.__func__, GETATTR_PRIVATE_BASELINE),
    ])
    def test_ratchet(self, rule_name, getter, baseline):
        actual: dict[str, int] = {}
        for py in self._iter_core_py():
            rel = py.relative_to(ROOT).as_posix()
            count = getter(ast.parse(py.read_text(encoding="utf-8")))
            if count:
                actual[rel] = count
        grown = {
            f: (baseline.get(f, 0), c)
            for f, c in actual.items()
            if c > baseline.get(f, 0)
        }
        assert not grown, (
            f"{rule_name}：新增违例 (基线,实际)={grown}。"
            "请沿概念边界抽取端口/协作者，而不是把方法拆成游离函数或伸手进"
            "协作者私有成员（见 docs/架构总览.md 六边形纪律）。"
        )
        shrunk = {
            f: (baseline[f], c)
            for f, c in baseline.items()
            if actual.get(f, 0) < c or c == 0
        }
        assert not shrunk, (
            f"{rule_name}：违例已减少但基线未收紧 (基线,实际)={shrunk}，"
            "把基线数字调低以固化成果。"
        )


class TestSecureFetchSingleImplementation:
    """SSRF 抓取循环（手动重定向 + 逐跳复检）全仓只允许存在于
    adapters/external/secure_fetch.py。

    历史教训：同一安全逻辑曾有 4 份 async + 1 份 sync 拷贝，且 sync 份的
    重定向语义与其他份分歧（直接拒绝 vs 逐跳复检）。任何加固要改多处，
    漏一处即漏洞。download_proxy 的 socket 流式代理循环节点例外（不同
    sink），已单列并锁定不再增长。
    """

    LOOP_PATTERNS = ("follow_redirects=False", "is_redirect")
    MIGRATED_FILES = (
        "core/application/transfer.py",
        "core/application/files/download.py",
        "core/application/ingest/video.py",
        "core/application/download_server_io.py",
    )

    def test_migrated_sites_route_through_secure_fetch(self):
        for rel in self.MIGRATED_FILES:
            content = (ROOT / rel).read_text(encoding="utf-8")
            for pat in self.LOOP_PATTERNS:
                assert pat not in content, \
                    f"{rel} 重新出现手写重定向循环 ({pat})——请调 secure_fetch"
            if rel != "core/application/transfer.py":
                assert "secure_fetch" in content, \
                    f"{rel} 不再引用 secure_fetch——抓取被改回了本地实现？"

    def test_only_secure_fetch_defines_the_hop_loop(self):
        loops = []
        for py in sorted((ROOT / "core" / "application").rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            content = py.read_text(encoding="utf-8")
            if "follow_redirects=False" in content:
                loops.append(py.relative_to(ROOT).as_posix())
        assert loops == ["core/application/download_proxy.py"], (
            f"手写抓取循环节点增多: {loops}（新 sink 需求请扩展 secure_fetch）"
        )
