"""⑭ store 的 __getattr__ 转发表：静态化 + 重名即硬错。

背景：`SqliteMetaStore.__getattr__` 与 `StorePart.__getattr__` 曾共用同一套
"按 `_PARTS` 声明顺序取**第一个**命中"的转发规则 —— 两个 part 定义同名方法时，
后者会被**静默遮蔽**（不报错、不变红、方法体永不执行）。本仓把这类
"静默的功能丧失"看得比红灯更重。

改造后：转发名 → part 的映射在 import 时静态建立（`build_method_map`），
重名直接 `RuntimeError`；两处 `__getattr__` 都退化为一次 dict 查找。
"""
from __future__ import annotations

import pytest

from adapters.persistence.sqlite.state import build_method_map


def test_duplicate_forwarded_name_is_a_hard_error():
    """同名方法被两个 part 定义 ⇒ 必须硬错，而不是静默取第一个。"""

    class _A:
        def shared(self) -> str:
            return "a"

    class _B:
        def shared(self) -> str:
            return "b"

    with pytest.raises(RuntimeError, match="duplicate forwarded method 'shared'"):
        build_method_map((("_a", _A), ("_b", _B)))


def test_private_names_are_never_forwarded():
    """下划线开头的方法不进表（与 `__getattr__` 的早退规则一致）。"""

    class _A:
        def _hidden(self) -> str:
            return "a"

        def shown(self) -> str:
            return "a"

    assert build_method_map((("_a", _A),)) == {"shown": "_a"}


def test_map_is_not_empty_and_every_part_contributes():
    """反空断言：表为空 ⇒ 两处 __getattr__ 静默失效；有 part 零贡献 ⇒ 它整个被漏掉。"""
    from adapters.persistence.sqlite.store import SqliteMetaStore, _PARTS

    owner = SqliteMetaStore._METHOD_OWNER
    assert owner, "转发表为空——__getattr__ 会静默失效"
    by_attr = dict(_PARTS)
    assert set(owner.values()) == set(by_attr), "有 part 一个转发名都没进表"
    for name, attr in owner.items():
        assert not name.startswith("_"), f"{name} 不该进转发表"
        assert hasattr(by_attr[attr], name), f"{name} 不在 {attr} 上"


def test_static_map_matches_the_legacy_first_match_rule():
    """静态表必须与旧 `__getattr__` 的"按 _PARTS 顺序取第一个命中"**逐名等价**。

    等价 ⇒ 本次静态化是纯重构（无行为变化）；不等价 ⇒ 有转发名被静默改道。
    """
    from adapters.persistence.sqlite.store import SqliteMetaStore, _PARTS

    legacy: dict[str, str] = {}
    for attr, part_cls in _PARTS:
        for name in dir(part_cls):
            if name.startswith("_") or not hasattr(part_cls, name):
                continue
            legacy.setdefault(name, attr)  # 第一个命中即胜出 == 旧语义
    assert legacy, "未抽取到任何转发名（反空断言）"
    assert legacy == SqliteMetaStore._METHOD_OWNER


async def test_cross_part_forwarding_still_resolves(tmp_path):
    """端到端：`StorePart.__getattr__` 的跨 part 转发必须仍然可用。

    `upsert_resources` 定义在 ResourcesMixin 上、FoldersMixin 上**没有**；
    folders 的代码直接 `self.upsert_resources(...)` ⇒ 必然走 `StorePart.__getattr__`。
    **只测静态表测不到这条路径** —— 曾漏掉这条守卫，代价是 4 个 reconcile
    用例直到跑门禁才炸出来（`owner._METHOD_OWNER` 取不到）。
    """
    from adapters.persistence.sqlite import SqliteMetaStore

    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    try:
        folders = s.__dict__["_folders"]
        assert not hasattr(type(folders), "upsert_resources"), "前提变了：folders 自己也有了"
        assert callable(folders.upsert_resources), "跨 part 转发失效（StorePart.__getattr__）"
        await s.upsert_album_essence("g1", [{"album_id": "a1", "name": "旅行"}], [])
        assert callable(s.upsert_resources), "聚合层转发失效（SqliteMetaStore.__getattr__）"
    finally:
        await s.close()


def test_shadow_bases_match_parts():
    """TYPE_CHECKING 影子继承的基类列表必须与 `_PARTS` 逐一对应且同序。

    影子类只存在于静态视图（IDE/pyright 导航）；一旦有人给组合新增/删除
    part 而忘同步影子声明，静态视图就会与运行时转发面漂移——本测试是钉子。
    """
    import ast
    from pathlib import Path

    from adapters.persistence.sqlite import store as store_mod

    tree = ast.parse(Path(store_mod.__file__).read_text(encoding="utf-8"))
    shadow_bases: list[str] = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.If)
                and isinstance(child.test, ast.Name)
                and child.test.id == "TYPE_CHECKING"
            ):
                for sub in child.body:
                    if isinstance(sub, ast.ClassDef) and sub.name == "SqliteMetaStore":
                        shadow_bases.extend(
                            b.id for b in sub.bases if isinstance(b, ast.Name)
                        )
            walk(child)

    walk(tree)
    assert shadow_bases, "未找到 TYPE_CHECKING 影子类声明（形状已变？）"
    part_classes = [cls.__name__ for _attr, cls in store_mod._PARTS]
    assert shadow_bases == part_classes, (
        f"影子基类 {shadow_bases} != _PARTS 声明序 {part_classes}"
    )


def test_port_surface_is_fully_forwarded():
    """MetaStorePort 声明的每个方法都必须能被运行时解析（转发表或类本体）。

    端口→实现的断链在动态派发下没有静态工具能提前发现，只能在调用点炸
    AttributeError；这里用一次纯反射把整张端口钉死。
    """
    import inspect

    from adapters.persistence.sqlite.store import SqliteMetaStore
    from ports.meta_store import MetaStorePort

    owner = SqliteMetaStore._METHOD_OWNER
    port_methods = [
        name for name, _ in inspect.getmembers(MetaStorePort, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert port_methods, "端口未抽到任何方法（反空断言）"
    missing = [
        m for m in port_methods
        if m not in owner and not hasattr(SqliteMetaStore, m)
    ]
    assert not missing, f"端口方法无法被解析（实现漂移）: {missing}"
