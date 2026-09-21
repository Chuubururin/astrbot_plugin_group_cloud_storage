"""file_type 分类器单元测试：静态表、多层后缀剥离、显示/过滤一致性。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.domain.file_type import (  # noqa: E402
    FILE_TYPE_EXT,
    _EXT_2_TYPE,
    classify,
    classify_with_overrides,
    normalize_type,
    type_exts,
    type_exts_with_overrides,
)


@pytest.mark.parametrize(
    ("name", "want"),
    [
        # 基础表命中 + 大小写
        ("a.docx", "document"),
        ("b.MP4", "video"),
        ("a.zip", "archive"),
        ("a.exe", "installer"),
        # 静态表扩充抽查
        ("iptv.m3u", "video"),
        ("live.m3u8", "video"),
        ("clip.m4v", "video"),
        ("song.opus", "audio"),
        ("book.epub", "document"),
        ("disk.iso", "archive"),
        ("backup.tgz", "archive"),
        ("app.ipa", "installer"),
        ("pkg.whl", "installer"),
        ("photo.jfif", "image"),
        ("design.psd", "image"),
        ("sheet.ods", "spreadsheet"),
        ("deck.key", "slide"),
        # 回退
        ("README", "other"),
        ("probe.bin", "other"),
        ("41d6b4ad-2b89-4c1d-a1b2-3c4d5e6f7a8b", "other"),
        (".bashrc", "other"),
        ("", "other"),
        ("file.p", "other"),
    ],
)
def test_classify_static_table(name, want):
    assert classify(name) == want


@pytest.mark.parametrize(
    ("name", "want"),
    [
        # 真实样本：SnowLuma 网盘下载中间态（meta.db 生产数据）
        ("新版本ensp套装.rar.netdisk.p.downloading", "archive"),
        # 本项目分卷格式直接命中
        ("Deep Layered Brown Noise ( 12 Hours ).part01of08.zip", "archive"),
        # 浏览器/工具临时后缀
        ("movie.mkv.crdownload", "video"),
        ("report.pdf.tmp", "pdf"),
        ("report.pdf.temp", "pdf"),
        ("file.rar.part", "archive"),
        ("file.rar.partial", "archive"),
        ("doc.docx.download", "document"),
        # 分卷序号后缀
        ("backup.zip.001", "archive"),
        ("backup.zip.999", "archive"),
        ("media.mkv.z01", "video"),
        # 多层叠加不超过剥离上限
        ("x.tar.netdisk.p.downloading", "archive"),
    ],
)
def test_classify_strips_transient_and_volume_suffixes(name, want):
    assert classify(name) == want


# 关键扩展名的期望分类：写成独立字面量，不从实现表 _EXT_2_TYPE 推导。
# 旧版拿实现表当期望值，只能发现“两张表互相不一致”，分类本身整体改错
# （例如 .zip 被归到 video）仍然全绿。
_LITERAL_EXT_TYPE = {
    ".zip": "archive",
    ".rar": "archive",
    ".7z": "archive",
    ".tgz": "archive",
    ".iso": "archive",
    ".mp4": "video",
    ".mkv": "video",
    ".m3u8": "video",
    ".mp3": "audio",
    ".flac": "audio",
    ".opus": "audio",
    ".docx": "document",
    ".epub": "document",
    ".txt": "document",
    ".pdf": "pdf",
    ".xlsx": "spreadsheet",
    ".ods": "spreadsheet",
    ".pptx": "slide",
    ".key": "slide",
    ".png": "image",
    ".jpg": "image",
    ".psd": "image",
    ".exe": "installer",
    ".apk": "installer",
    ".whl": "installer",
}


def test_classify_strip_is_additive():
    """剥离只影响原本归 other 的名字：已知扩展名叠加临时/分卷后缀后类型不变。"""
    suffix_adds = [
        ".downloading", ".tmp", ".part", ".crdownload",
        ".netdisk.p", ".netdisk.p.downloading", ".001", ".z01",
    ]
    for ext, want in _LITERAL_EXT_TYPE.items():
        for add in suffix_adds:
            assert classify(f"x{ext}{add}") == want, f"x{ext}{add}"
    # 全表自洽：表内每个扩展名都能被 classify 认出（不涉及具体分类对错）
    for ext, want in _EXT_2_TYPE.items():
        assert classify(f"x{ext}") == want, f"x{ext}"


def test_classify_strip_stops_at_first_known_ext():
    """第一层命中已知类型即返回，不再继续剥离（避免过度剥离改变语义）。"""
    assert classify("a.zip.bak") == "other"  # .bak 非临时后缀，不剥
    assert classify("a.zip.txt") == "document"  # .txt 已知：第一层即命中，不剥到 .zip


def test_type_exts_table_consistency():
    """静态表无重复扩展名、全部小写带点，且关键扩展名分类与字面量一致。"""
    seen: set[str] = set()
    for exts in FILE_TYPE_EXT.values():
        for e in exts:
            assert e.startswith(".") and e == e.lower()
            assert e not in seen, f"duplicate ext {e}"
            seen.add(e)
    # 旧版此处断言 _EXT_2_TYPE[e] == ftype，而 _EXT_2_TYPE 就是由 FILE_TYPE_EXT
    # 派生的：同源断言，恒真。改成字面量期望，分类表整体改错也能变红。
    for ext, want in _LITERAL_EXT_TYPE.items():
        assert ext in seen, f"关键扩展名 {ext} 从静态表中消失"
        assert _EXT_2_TYPE[ext] == want, f"{ext} 应为 {want}，实际 {_EXT_2_TYPE[ext]}"
        assert ext in FILE_TYPE_EXT[want], f"{ext} 未出现在 FILE_TYPE_EXT[{want!r}] 中"


def test_classify_with_overrides_priority():
    """配置覆盖优先于静态表与剥离；未命中覆盖时回退到剥离后的 classify。"""
    ov = {".zip": "video", "mkv": "audio"}
    assert classify_with_overrides("a.zip", ov) == "video"
    assert classify_with_overrides("a.mkv", ov) == "audio"
    assert classify_with_overrides("a.png", ov) == "image"
    # 覆盖键匹配的是最后一级后缀，不参与剥离
    assert classify_with_overrides("a.zip.001", ov) == "archive"
    # 无覆盖时与 classify 一致
    assert classify_with_overrides("x.rar.netdisk.p.downloading", None) == "archive"


def test_type_exts_with_overrides_union():
    exts = type_exts_with_overrides("video", {".m3u": "video", ".txt": "document"})
    assert ".m3u" in exts and ".mp4" in exts
    assert ".txt" not in exts
    assert type_exts_with_overrides("video", None) == type_exts("video")


def test_normalize_type_legacy_aliases():
    assert normalize_type("program") == "installer"
    assert normalize_type("data") == "other"
    assert normalize_type("video") == "video"
    assert normalize_type(None) == "other"
