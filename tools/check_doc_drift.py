"""文档 / schema / 路由 / 前端 API 漂移检查。

把"最大的维护风险"——**文档、配置 schema、路由表、前端 API 常量之间的漂移**
——从"靠人记"变成"红灯"。与 `tools/check_import_contracts.py` 同形：纯静态 AST/文本分析，
不 import 任何插件代码（因此不需要 astrbot 运行时）。

契约
----
K1  `DEFAULTS`（`core/config/defaults.py`，代码真相源）== `_conf_schema.json` 键集
K2  `DEFAULTS` == `docs/配置项总表.md` 键集
K3  `_RELOAD_REQUIRED_KEYS`（`webapi/config.py`）⊆ `DEFAULTS`
R1  `ROUTES` 的 handler 名都能在 `webapi/` 下静态找到定义
R2  `ROUTES` 的 suffix 都出现在 `docs/接口契约.md` 的「REST 端点总表」里
R3  `pages/storage-ng/api.js` 的每个路径都能对应到 `ROUTES` 的 suffix
K5  `docs/**/*.md` 不出现 `DEAD_SYMBOLS`（已删除的模块/类名）

允许清单（每一条都必须写理由，且尽量短）
----------------------------------------
* `SCHEMA_EXEMPT`   —— 故意不进 AstrBot 配置 schema 的键（已废弃别名，仅向后兼容）
* `DOC_EXEMPT`      —— 故意不写进文档的键（凭据类）
* `API_DYNAMIC`     —— 前端按模板拼出的路径前缀（对应带 `<param>` 的动态路由）

R2 额外接受「文档只写末段」（如 `files/batch-move` 在表中记作 `batch-move`）；
全段与末段匹配都带路径段边界（见 `_documented`），前缀不得搭车
（`files/link` 不因文档写了 `files/links` 而通过）。

Usage: python3 tools/check_doc_drift.py [repo_root]   (exit 1 with violation details)
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS_PY = "core/config/defaults.py"
SCHEMA_JSON = "_conf_schema.json"
CONF_MD = "docs/配置项总表.md"
CONTRACT_MD = "docs/接口契约.md"
ROUTES_PY = "webapi/routes.py"
CONF_PY = "webapi/config.py"
API_JS = "pages/storage-ng/api.js"

REST_SECTION = "## REST 端点总表"

# --- 允许清单（每条都有理由） ---

# 已废弃的字节单位别名：只读兼容，不进 AstrBot 配置页（写进去会诱导用户填错单位）
SCHEMA_EXEMPT: frozenset[str] = frozenset({
    "request_interval_ms",
    "volume_threshold_mb",
    "fetch_max_bytes",
    "bridge_min_bytes",
    "bridge_max_bytes",
})

# 凭据类：文档总表按项目约定不逐字列出取值（只说明用途与脱敏），故与 schema 键集对齐后允许缺席
DOC_EXEMPT: frozenset[str] = frozenset()

# 前端把 `<token>` 之类动态段拼在路径后（api.js 只存前缀）
API_DYNAMIC: frozenset[str] = frozenset({"files/upload"})

# K4：插件名字面量只允许一个定义点（routes.py）。
# 用 f-string 拼装：避免本文件源码里出现该字面量，否则 K4 会把自己也算成一次定义。
_PLUGIN_NAME_VALUE = "astrbot_plugin_group_cloud_storage"
PLUGIN_NAME_LITERAL = f'PLUGIN_NAME = "{_PLUGIN_NAME_VALUE}"'


# ---------------------------------------------------------------- 抽取

def _ast_assign_value(path: Path, name: str) -> ast.expr:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return node.value
    raise SystemExit(f"ABORT: {path} 里找不到 {name} 赋值")


def defaults_keys(root: Path) -> list[str]:
    value = _ast_assign_value(root / DEFAULTS_PY, "DEFAULTS")
    if not isinstance(value, ast.Dict):
        raise SystemExit(f"ABORT: {DEFAULTS_PY} 的 DEFAULTS 不是字面量 dict")
    out = []
    for k in value.keys:
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            raise SystemExit(f"ABORT: {DEFAULTS_PY} 出现非字符串键: {ast.dump(k)[:60]}")
        out.append(k.value)
    return sorted(out)


def routes_table(root: Path) -> list[tuple[str, tuple[str, ...], str]]:
    value = _ast_assign_value(root / ROUTES_PY, "ROUTES")
    if not isinstance(value, ast.List):
        raise SystemExit(f"ABORT: {ROUTES_PY} 的 ROUTES 不是字面量 list")
    out = []
    for elt in value.elts:
        if (
            not isinstance(elt, ast.Call)
            or not isinstance(elt.func, ast.Name)
            or elt.func.id != "Route"
        ):
            raise SystemExit(
                f"ABORT: {ROUTES_PY} 存在非 Route(...) 条目: {ast.dump(elt)[:80]}"
            )
        if len(elt.args) != 5:
            raise SystemExit(
                f"ABORT: {ROUTES_PY} Route(...) 实参不是 5 个: {ast.dump(elt)[:80]}"
            )
        suffix, methods, handler = elt.args[0], elt.args[1], elt.args[2]
        if not isinstance(suffix, ast.Constant) or not isinstance(handler, ast.Constant):
            raise SystemExit(f"ABORT: {ROUTES_PY} 条目含非常量字段: {ast.dump(elt)[:80]}")
        if not isinstance(methods, ast.Tuple):
            raise SystemExit(f"ABORT: {ROUTES_PY} methods 不是字面量 tuple: {suffix.value}")
        out.append((
            suffix.value,
            tuple(str(m.value) for m in methods.elts),
            handler.value,
        ))
    # 反空断言：抽取到 0 条时 R1/R2/R3 会全部空转通过 —— 那是"静默的功能丧失"。
    if not out:
        raise SystemExit(f"ABORT: {ROUTES_PY} 未抽取到任何路由（AST 形状已变？）")
    return out


def webapi_defs(root: Path) -> set[str]:
    """webapi/ 下所有模块级 def/class/赋值名（handler 的静态定义面）。"""
    names: set[str] = set()
    for p in sorted((root / "webapi").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def reload_required(root: Path) -> list[str]:
    text = (root / CONF_PY).read_text(encoding="utf-8")
    m = re.search(r"_RELOAD_REQUIRED_KEYS[^=]*=\s*frozenset\(\{(.*?)\}\)", text, re.S)
    if not m:
        raise SystemExit(f"ABORT: {CONF_PY} 里找不到 _RELOAD_REQUIRED_KEYS frozenset")
    return sorted(set(re.findall(r'"([^"]+)"', m.group(1))))


def schema_keys(root: Path) -> list[str]:
    return sorted(json.loads((root / SCHEMA_JSON).read_text(encoding="utf-8")))


def doc_row_keys(root: Path) -> list[str]:
    """配置项总表里**每个表格行首列**的反引号标识符（兼容 `a` / `b` 合并单元格）。

    K2 两个方向都用它，因此要求**每个配置键在文档里都有自己的行**。
    ⚠️ 不能用"全文提到即可"的宽判据：那样删掉某键的专属行也不会变红
    （它往往在别处被顺带提及）—— 反向验证已实测坐实这一点。
    """
    keys: set[str] = set()
    for line in (root / CONF_MD).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        parts = line.split("|")
        cell = parts[1] if len(parts) > 1 else ""
        for tok in re.findall(r"`([^`]+)`", cell):
            if re.fullmatch(r"[a-z][a-z0-9_]*", tok):
                keys.add(tok)
    return sorted(keys)


def rest_section(root: Path) -> str:
    text = (root / CONTRACT_MD).read_text(encoding="utf-8")
    start = text.find(REST_SECTION)
    if start < 0:
        raise SystemExit(f"ABORT: {CONTRACT_MD} 里找不到「{REST_SECTION}」章节")
    end = text.find("\n## ", start + len(REST_SECTION))
    return text[start:] if end < 0 else text[start:end]


def api_js_paths(root: Path) -> list[str]:
    text = (root / API_JS).read_text(encoding="utf-8")
    m = re.search(r"export const API = \{(.*?)\n\};", text, re.S)
    if not m:
        raise SystemExit(f"ABORT: {API_JS} 里找不到 API 常量表块")
    out: set[str] = set()
    for line in m.group(1).splitlines():
        code = line.split("//", 1)[0]
        for val in re.findall(r":\s*'([^']*)'", code):
            if val:
                out.add(val)
    return sorted(out)


def _documented(section: str, suffix: str) -> bool:
    """suffix 是否以完整路径段出现在端点总表里。

    前后都加边界（前面不是 词字符//，后面不是 词字符//或 -）：裸子串
    匹配会让前缀搭车——`files/link` 因文档写了 `files/links` 而漏判为
    已收录。末段回退（文档只写 `batch-move`）同样带边界。
    """
    def _hit(token: str) -> bool:
        pat = rf"(?<![\w/]){re.escape(token)}(?![\w/-])"
        return re.search(pat, section) is not None

    return _hit(suffix) or _hit(suffix.split("/")[-1])


# 已删除的符号：文档不得再把它当作在役组件描述（"删了代码留了文档"是最典型的漂移）
# 每条都要写清"为什么删"，便于后来者判断是不是又该加回来。
DEAD_SYMBOLS: dict[str, str] = {
    "StorageGateway": "core/application/gateway.py 已删除：零调用方，且 egress()/probe_target() "
                      "调用的 TransferService 方法在仓库中不存在",
}

# ---------------------------------------------------------------- 检查

def check(root: Path) -> list[str]:
    bad: list[str] = []

    dk = set(defaults_keys(root))
    sk = set(schema_keys(root))
    rows = set(doc_row_keys(root))
    rk = set(reload_required(root))

    # K1
    for key in sorted(dk - sk - SCHEMA_EXEMPT):
        bad.append(f"K1 {key}: 在 DEFAULTS 里但不在 _conf_schema.json（未列入 SCHEMA_EXEMPT）")
    for key in sorted(sk - dk):
        bad.append(f"K1 {key}: 在 _conf_schema.json 里但不在 DEFAULTS（schema 键必须有默认值）")

    # K2 正向：代码里的每个键在文档里都要有自己的行
    for key in sorted(dk - rows - DOC_EXEMPT):
        bad.append(f"K2 {key}: 在 DEFAULTS 里但 docs/配置项总表.md 没有它的行")
    # K2 反向：文档行声明的键必须真实存在
    for key in sorted(rows - dk):
        bad.append(f"K2 {key}: docs/配置项总表.md 记载了但 DEFAULTS 里不存在（文档陈旧）")

    # K3
    for key in sorted(rk - dk):
        bad.append(f"K3 {key}: _RELOAD_REQUIRED_KEYS 里的键不在 DEFAULTS")

    # K4 PLUGIN_NAME 只能有一个字面量定义（多处定义会静默漂移）
    literals = [
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*.py"))
        if ".git" not in p.parts and PLUGIN_NAME_LITERAL in p.read_text(encoding="utf-8")
    ]
    if len(literals) != 1:
        bad.append(
            "K4 PLUGIN_NAME 字面量应恰好出现 1 次，实际 " + str(len(literals))
            + " 次：" + ", ".join(literals)
        )

    # K5 docs/ 不得引用已删除的符号
    for doc in sorted((root / "docs").rglob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for sym, why in DEAD_SYMBOLS.items():
            if sym in text:
                bad.append(
                    f"K5 {doc.relative_to(root).as_posix()}: 引用了已删除的符号 {sym}（{why}）"
                )

    # R1
    defined = webapi_defs(root)
    routes = routes_table(root)
    for suffix, _methods, handler in routes:
        if handler not in defined:
            bad.append(f"R1 {suffix}: handler {handler} 在 webapi/ 下找不到定义")

    # R2
    section = rest_section(root)
    for suffix, _methods, _handler in routes:
        if _documented(section, suffix):
            continue
        bad.append(f"R2 {suffix}: docs/接口契约.md 的 REST 端点总表未收录")

    # R3
    suffixes = {s for s, _m, _h in routes}
    for path in api_js_paths(root):
        if path in suffixes or path in API_DYNAMIC:
            continue
        if any(s == path or s.startswith(path + "/") for s in suffixes):
            continue
        bad.append(f"R3 {path}: api.js 引用但 ROUTES 里没有对应 suffix")

    return bad


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else ROOT
    bad = check(root)
    if bad:
        print(f"文档/路由漂移检查失败（{len(bad)} 条）：")
        for line in bad:
            print(f"  - {line}")
        return 1
    print(
        "文档/路由漂移检查全部通过"
        f"（K1-K5 + R1-R3；DEFAULTS={len(defaults_keys(root))} 键，"
        f"ROUTES={len(routes_table(root))} 条，"
        f"DEAD_SYMBOLS={len(DEAD_SYMBOLS)} 条）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
