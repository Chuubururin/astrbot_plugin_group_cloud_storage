"""跨平台回归：分卷临时目录名必须在 Windows 上合法。

真实缺陷（2026-10 发现）：parent_id 形如 "volgroup:<hex>"，旧代码直接
`tmp_dir / f"vol_{parent_id}"`，':' 在 Windows 是非法文件名字符 ->
每次大文件分卷都 WinError 267，队列无限重试。CI 只跑 Linux 从未暴露，
所以本文件的判据不依赖运行平台：只断言名字的字符集契约。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.files.volume import _cut_dir_name  # noqa: E402

# Windows 非法文件名字符 + 路径分隔符
_WIN_FORBIDDEN = set(':*?"<>|/\\')
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_NASTY_IDS = [
    "volgroup:625d805149",          # 真实分卷 id
    "42",                            # convert_volumes 走数字 id
    "a:b|c<d>e:f?g*h",               # 全量非法字符
    "../../escape",                  # 路径穿越
    "trailing.dots...",              # Windows 会剥掉结尾点
    "CON",                           # 保留设备名
    "x" * 200,                       # 超长 id
    "volgroup/625d805149",           # 与 "volgroup:625d805149" sanitize 后同 slug
]


@pytest.mark.parametrize("parent_id", _NASTY_IDS)
def test_cut_dir_name_is_windows_legal(parent_id):
    name = _cut_dir_name(parent_id)
    assert name, "目录名不得为空"
    assert _SAFE_NAME_RE.match(name), f"含非法字符: {name!r}"
    assert not (set(name) & _WIN_FORBIDDEN), f"含 Windows 非法字符: {name!r}"
    assert name == name.rstrip(". "), f"结尾点/空格在 Windows 会被剥掉: {name!r}"
    assert name != "..", f"路径穿越面: {name!r}"
    assert name.split(".", 1)[0].upper() not in _RESERVED, f"保留设备名: {name!r}"
    assert len(name) <= 80, f"目录名过长，逼近 MAX_PATH: {len(name)}"


def test_cut_dir_name_is_deterministic():
    # 一次上传有三处派生点（split/异常清理/finally 清理），必须得到同一名字
    assert _cut_dir_name("volgroup:625d805149") == _cut_dir_name("volgroup:625d805149")


def test_cut_dir_name_no_collision_after_sanitize():
    # ':' 与 '/' 清洗后 slug 相同，但必须落到不同目录，
    # 否则两个父资源的切片互相覆盖
    a = _cut_dir_name("volgroup:625d805149")
    b = _cut_dir_name("volgroup/625d805149")
    assert a != b


def test_cut_dir_name_survives_mkdir(tmp_path):
    # 名字本身可被创建（Linux 上也顺手验证格式没退化出控制字符）
    d = tmp_path / _cut_dir_name("volgroup:625d805149")
    d.mkdir(parents=True, exist_ok=True)
    assert d.is_dir()
