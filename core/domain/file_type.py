"""文件类型字典（2026-09-01 所有者定稿：13 类分类，ADR-0008 N-01）。

- `classify(name)` -> 分组名（未识别 → other）
- `FILE_TYPE_EXT` -> 分组 → [扩展名]（含点，小写），供 SQL 后缀筛选
- 存量别名：`program` -> `installer`、`data` -> `other`（normalize_type 归一，
  兼容历史 type_ext_overrides 配置与存量 type 列；存量行不重写）

13 类机器值：document/pdf/spreadsheet/slide/online_doc/image/video/audio/
archive/installer/flash/folder/other（folder 恒为目录行类别，不参与扩展名判定）。
"""

from __future__ import annotations

from pathlib import Path

FILE_TYPE_EXT: dict[str, list[str]] = {
    "document": [".doc", ".docx", ".odt", ".rtf", ".wps", ".txt", ".md"],
    "pdf": [".pdf"],
    "spreadsheet": [".xls", ".xlsx", ".et", ".csv"],
    "slide": [".ppt", ".pptx", ".dps"],
    "online_doc": [],  # 无固定后缀；配置 type_ext_overrides 可增补
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"],
    "video": [".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"],
    "audio": [".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"],
    "archive": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
    "installer": [".exe", ".msi", ".apk", ".deb", ".rpm", ".dmg", ".appimage"],
    "flash": [],  # 无固定后缀；配置 type_ext_overrides 可增补
    "folder": [],  # 目录行类别（N-03）；不参与扩展名判定
    "other": [],
}

FILE_TYPE_LABEL: dict[str, str] = {
    "document": "文稿",
    "pdf": "PDF",
    "spreadsheet": "表格",
    "slide": "幻灯片",
    "online_doc": "在线文档",
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "archive": "压缩包",
    "installer": "安装包",
    "flash": "闪传文件",
    "folder": "文件夹",
    "other": "其他",
}

# 存量类型别名（只增不减：历史配置/存量 type 列经此归一）
TYPE_ALIASES: dict[str, str] = {
    "program": "installer",
    "data": "other",
}

KNOWN_TYPES: list[str] = [k for k in FILE_TYPE_EXT]

_EXT_2_TYPE: dict[str, str] = {}
for _t, _exts in FILE_TYPE_EXT.items():
    for _e in _exts:
        _EXT_2_TYPE[_e] = _t


def normalize_type(ftype: str | None) -> str:
    """存量类型归一（program→installer、data→other）；未知原样返回。"""
    if not ftype:
        return "other"
    return TYPE_ALIASES.get(ftype, ftype)


def classify(name: str) -> str:
    """按文件名返回类型分组（other 兜底）。"""
    ext = Path(name or "").suffix.lower()
    return _EXT_2_TYPE.get(ext, "other")


def type_exts(ftype: str) -> list[str]:
    """类型分组 → 扩展名列表（含点）；未知组/别名组返回 []（别名经 normalize_type 归一）。"""
    return FILE_TYPE_EXT.get(normalize_type(ftype), [])


def type_label(ftype: str) -> str:
    return FILE_TYPE_LABEL.get(normalize_type(ftype), ftype)


# ---------- CT-9 分类与预览可配置（N6：数据驱动默认表 + 配置覆盖） ----------

# 预览策略默认表：类型组 -> {mode: builtin|external|download, template}
# external 模板含 {src} 占位符（替换为直链后打开）；空 template = 直链直接打开
DEFAULT_PREVIEW_POLICY: dict[str, dict] = {
    "document": {"mode": "external", "template": ""},
    "pdf": {"mode": "external", "template": ""},
    "spreadsheet": {"mode": "external", "template": ""},
    "slide": {"mode": "external", "template": ""},
    "online_doc": {"mode": "external", "template": ""},
    "image": {"mode": "builtin"},
    "video": {"mode": "builtin"},
    "audio": {"mode": "builtin"},
    "album": {"mode": "builtin"},
    "essence": {"mode": "builtin"},
    "archive": {"mode": "download"},
    "installer": {"mode": "download"},
    "flash": {"mode": "download"},
    "folder": {"mode": "download"},
    "other": {"mode": "download"},
}


def preview_policy_for(ftype: str, policy_overrides: dict | None = None) -> dict:
    """类型组 → 预览策略；overrides（配置键 preview_policy）按组合并覆盖。"""
    policy = dict(
        DEFAULT_PREVIEW_POLICY.get(normalize_type(ftype), DEFAULT_PREVIEW_POLICY["other"])
    )
    for ov in (policy_overrides or {}).values():
        if not isinstance(ov, dict):
            continue
        if str(ov.get("types", "")).split(",") and ftype in [
            t.strip() for t in str(ov.get("types", "")).split(",") if t.strip()
        ]:
            if ov.get("mode"):
                policy["mode"] = ov["mode"]
            if ov.get("template") is not None:
                policy["template"] = ov["template"]
    return policy


def classify_with_overrides(name: str, ext_overrides: dict | None = None) -> str:
    """按文件名分类；ext_overrides（配置键 type_ext_overrides：{".xyz": "video"}）优先。"""
    ext = Path(name or "").suffix.lower()
    if ext_overrides:
        hit = ext_overrides.get(ext) or ext_overrides.get(ext.lstrip("."))
        if hit:
            return normalize_type(str(hit))
    return classify(name)