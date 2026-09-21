"""main.py /cssave 参数解析回归（M25）。

docs/接口契约.md 明确“正文可含空格”；旧实现用 rest.split(" ", 2) 最多切
三段，无群号时 title 弹掉后 text 只取第 2 段，第 3 段起被静默丢弃。

main.py 不能在测试桩下直接 import（core.runtime.adapter 依赖宿主
astrbot.core 包，且 import 时会 evict sys.modules 中的插件模块，影响其他
用例），所以这里用 AST 取出真实函数体单独执行：测的仍是生产代码本身。

Run: pytest tests/unit/test_main_cssave_parse.py -v
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


def _load_parser():
    """从 main.py 源码中提取 _parse_cssave_args 并编译执行。"""
    tree = ast.parse(_MAIN_SRC, filename="main.py")
    node = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "_parse_cssave_args"),
        None,
    )
    assert node is not None, "main.py 必须保留模块级 _parse_cssave_args"
    namespace: dict = {}
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, "main.py", "exec"), namespace)  # noqa: S102
    return namespace["_parse_cssave_args"]


parse = _load_parser()
BODY = "本周完成 A 和 B，下周做 C"


def test_cssave_without_group_keeps_whole_body():
    """无群号：正文含空格必须完整保留（反向验证：修复前只剩“本周完成”）。"""
    assert parse(f"周报 {BODY}") == ("", "周报", BODY)


def test_cssave_with_group_keeps_whole_body():
    """带群号：群号 + 标题 + 含空格正文。"""
    assert parse(f"123456 周报 {BODY}") == ("123456", "周报", BODY)


def test_cssave_single_space_body_no_truncation():
    """回归原始症状：正文有两个词时，第二个词不得被丢掉。"""
    assert parse("标题 正文甲 正文乙") == ("", "标题", "正文甲 正文乙")


def test_cssave_partial_forms():
    assert parse("") == ("", "", "")
    assert parse("123456") == ("123456", "", "")
    assert parse("123456 标题") == ("123456", "标题", "")
    # 短数字不是群号（与既有“群号 ≥ 5 位”规则一致），整体当作标题
    assert parse("1234 标题 正文") == ("", "1234", "标题 正文")


# ---------------------------------------------------------------------------
# handler 行为：cssave 必须走解析函数（不得自己内联切片）
# ---------------------------------------------------------------------------

def _load_strip_command_params():
    """按文件路径加载 core/runtime/commands.py（绕开包 __init__ 的宿主依赖）。"""
    import importlib.util

    path = ROOT / "core" / "runtime" / "commands.py"
    spec = importlib.util.spec_from_file_location("_runtime_commands_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.strip_command_params


def _load_cssave_handler_module():
    """提取 Main.cssave（剥掉 @filter 装饰器）编译成模块。"""
    tree = ast.parse(_MAIN_SRC, filename="main.py")
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "Main")
    fn = next(n for n in cls.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "cssave")
    fn.decorator_list = []
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    return ast.fix_missing_locations(ast.Module(body=[future, fn], type_ignores=[]))


_strip_command_params = _load_strip_command_params()


class _Scope:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str

    def plain_result(self, result):
        return result


class _FakeSelf:
    class _Lifecycle:
        _inited = True

    _lifecycle = _Lifecycle()
    services = object()

    def _bot_scope(self, event):
        return _Scope()


@pytest.mark.asyncio
async def test_cssave_handler_parses_by_behavior():
    """行为断言：给定输入 -> handler 交给 handle_cssave 的参数必须正确。

    替代旧断言 ``assert "split(" not in body``：那是扫源码文本的实现细节断言，
    handler 里出现任何无关的 split(（日志、其他字段处理）都会误报。反向验证：
    把 handler 改回 rest.split(" ", 2) 内联切片，本用例收到的参数就不对。
    """
    seen: dict = {}

    async def _fake_handle_cssave(event, services, group_id, title, text):
        seen.update(group_id=group_id, title=title, text=text)
        return "ok"

    namespace = {
        "strip_command_params": _strip_command_params,
        "_parse_cssave_args": parse,
        "handle_cssave": _fake_handle_cssave,
        "AstrMessageEvent": object,
    }
    exec(compile(_load_cssave_handler_module(), "main.py", "exec"), namespace)  # noqa: S102
    cssave = namespace["cssave"]

    for raw, want in (
        ("/cssave 周报 " + BODY, ("", "周报", BODY)),
        ("/cssave 123456 周报 " + BODY, ("123456", "周报", BODY)),
        ("/cssave 标题 正文甲 正文乙", ("", "标题", "正文甲 正文乙")),
    ):
        seen.clear()
        async for _ in cssave(_FakeSelf(), _FakeEvent(raw)):
            pass
        assert (seen["group_id"], seen["title"], seen["text"]) == want, raw
