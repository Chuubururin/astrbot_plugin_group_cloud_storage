"""CT-9 分类与预览策略测试（N6：数据驱动默认表 + 配置覆盖，改表不改码）。

2026-09-01（ADR-0008 N-01）：分类 13 类机器值——document/pdf/spreadsheet/slide/
online_doc/image/video/audio/archive/installer/flash/folder/other；
存量别名 program→installer、data→other 经 normalize_type 归一。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.domain.file_type import (  # noqa: E402
    DEFAULT_PREVIEW_POLICY, FILE_TYPE_EXT, normalize_type, classify,
    classify_with_overrides, preview_policy_for, type_exts, type_label,
)


def test_classify_13_categories():
    # 13 类机器值全部在默认表中（folder 恒为目录行类别）
    expected = {
        "document", "pdf", "spreadsheet", "slide", "online_doc",
        "image", "video", "audio", "archive", "installer", "flash",
        "folder", "other",
    }
    assert set(FILE_TYPE_EXT) == expected
    # 扩展名判定
    assert classify("a.docx") == "document"
    assert classify("a.pdf") == "pdf"
    assert classify("a.xlsx") == "spreadsheet"
    assert classify("a.csv") == "spreadsheet"
    assert classify("a.pptx") == "slide"
    assert classify("b.MP4") == "video"
    assert classify("a.zip") == "archive"
    assert classify("a.exe") == "installer"
    assert classify("a.txt") == "document"
    assert classify("c.unknownxyz") == "other"


def test_classify_with_ext_overrides():
    ov = {".xyz": "video", "abc": "data"}
    assert classify_with_overrides("f.xyz", ov) == "video"
    # 无点键亦可命中；存量 data 经别名归一为 other
    assert classify_with_overrides("f.abc", ov) == "other"
    # 未命中的扩展名回落默认字典
    assert classify_with_overrides("f.pdf", ov) == "pdf"
    # 无覆盖时等价 classify
    assert classify_with_overrides("f.pdf", None) == classify("f.pdf")


def test_legacy_aliases_normalize():
    # program→installer、data→other（兼容存量配置与 type 列）
    assert normalize_type("program") == "installer"
    assert normalize_type("data") == "other"
    assert normalize_type("installer") == "installer"
    assert normalize_type("other") == "other"
    assert normalize_type(None) == "other"
    # 别名组不直接出现在默认表/扩展名查询
    assert "program" not in FILE_TYPE_EXT
    assert "data" not in FILE_TYPE_EXT
    assert type_exts("program") == type_exts("installer")
    assert type_label("program") == "安装包"
    assert type_label("data") == "其他"


def test_preview_policy_defaults():
    assert preview_policy_for("image")["mode"] == "builtin"
    assert preview_policy_for("document")["mode"] == "external"
    assert preview_policy_for("pdf")["mode"] == "external"
    assert preview_policy_for("spreadsheet")["mode"] == "external"
    assert preview_policy_for("archive")["mode"] == "download"
    assert preview_policy_for("installer")["mode"] == "download"
    # 存量别名预览策略归一
    assert preview_policy_for("program")["mode"] == "download"
    assert preview_policy_for("data")["mode"] == "download"
    # 未知类型兜底
    assert preview_policy_for("no-such-type")["mode"] == "download"


def test_preview_policy_overrides_merge():
    overrides = {
        "office": {"types": "document,spreadsheet", "mode": "external",
                   "template": "https://view.example/?url={src}"},
        "media": {"types": "video", "mode": "download"},
    }
    doc = preview_policy_for("document", overrides)
    assert doc["mode"] == "external"
    assert doc["template"] == "https://view.example/?url={src}"
    vid = preview_policy_for("video", overrides)
    assert vid["mode"] == "download"
    # 未覆盖的类型保持默认
    img = preview_policy_for("image", overrides)
    assert img["mode"] == "builtin"


def test_default_table_is_data_driven_shape():
    for t, policy in DEFAULT_PREVIEW_POLICY.items():
        assert policy.get("mode") in ("builtin", "external", "download"), t