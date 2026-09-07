"""Import contract checker (whitelist contracts) — an import-linter equivalent
for the plugin's top-level module layout.

The plugin uses a top-level module layout (core/, adapters/, ... at the plugin
root, imported via sys.path); the official import-linter requires a single
root package and cannot run here. This tool enforces the same contracts
via AST:

  C1 ports purity      ports must not import core.application / adapters
  C2 application whitelist
                       application may only reach adapters.external
                       (adapters.onebot / persistence / limiter / store
                       are forbidden)
  C3 domain bottom layer
                       core.domain must not import application/adapters/
                       webapi/commands
  C4 edge isolation    core.application must not import webapi / commands

Usage: python3 tools/check_import_contracts.py   (exit 1 with violation details)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Contract definitions: contract_id -> (description, source package, forbidden module prefixes)
CONTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "C1": (
        "ports 不依赖 application/adapters",
        "ports",
        ("core.application", "adapters"),
    ),
    "C2": (
        "application 适配器白名单（仅 adapters.external）",
        "core.application",
        ("adapters.onebot", "adapters.persistence", "adapters.limiter", "adapters.store"),
    ),
    "C3": (
        "domain 不依赖 application/adapters/webapi/commands",
        "core.domain",
        ("core.application", "adapters", "webapi", "commands"),
    ),
    "C4": (
        "application 不依赖 webapi/commands",
        "core.application",
        ("webapi", "commands"),
    ),
}


def imported_modules(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    return mods


def check_contract(cid: str, description: str, source_pkg: str, forbidden: tuple) -> list[str]:
    violations: list[str] = []
    src_dir = ROOT / Path(*source_pkg.split("."))
    if not src_dir.is_dir():
        return [f"[{cid}] 源目录缺失: {source_pkg}"]
    for py in sorted(src_dir.rglob("*.py")):
        rel = py.relative_to(ROOT)
        for mod in imported_modules(py):
            for bad in forbidden:
                if mod == bad or mod.startswith(bad + "."):
                    violations.append(f"[{cid}] {description}\n    {rel} -> {mod}")
    return violations


def main() -> int:
    all_violations: list[str] = []
    for cid, (desc, src, forbidden) in CONTRACTS.items():
        all_violations.extend(check_contract(cid, desc, src, forbidden))
    if all_violations:
        print(f"import 契约违规 {len(all_violations)} 处：")
        for v in all_violations:
            print(" " + v)
        return 1
    print(f"import 契约全部通过（{len(CONTRACTS)} 条：{', '.join(CONTRACTS)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
