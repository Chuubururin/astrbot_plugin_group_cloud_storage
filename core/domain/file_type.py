"""File type dictionary: 13-category classification.

- `classify(name)` -> group name (unknown -> other)
- `FILE_TYPE_EXT` -> group -> [extensions] (lowercase, with dot), for SQL
  suffix filters
- legacy aliases: `program` -> `installer`, `data` -> `other` (normalized by
  normalize_type, keeping older type_ext_overrides configs and stored type
  values working; existing rows are not rewritten)

Machine values for the 13 groups: document/pdf/spreadsheet/slide/online_doc/
image/video/audio/archive/installer/flash/folder/other (folder is always the
category of directory rows and never participates in extension matching).
"""

from __future__ import annotations

from pathlib import Path

FILE_TYPE_EXT: dict[str, list[str]] = {
    "document": [".doc", ".docx", ".odt", ".rtf", ".wps", ".txt", ".md"],
    "pdf": [".pdf"],
    "spreadsheet": [".xls", ".xlsx", ".et", ".csv"],
    "slide": [".ppt", ".pptx", ".dps"],
    "online_doc": [],  # no fixed extensions; extensible via the type_ext_overrides config
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"],
    "video": [".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"],
    "audio": [".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"],
    "archive": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
    "installer": [".exe", ".msi", ".apk", ".deb", ".rpm", ".dmg", ".appimage"],
    "flash": [],  # no fixed extensions; extensible via the type_ext_overrides config
    "folder": [],  # category of directory rows; not used for extension matching
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

# legacy type aliases (add-only: historical configs / stored type values are normalized here)
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
    """Normalize legacy types (program->installer, data->other); unknown passes through."""
    if not ftype:
        return "other"
    return TYPE_ALIASES.get(ftype, ftype)


def classify(name: str) -> str:
    """Return the type group for a file name (falls back to other)."""
    ext = Path(name or "").suffix.lower()
    return _EXT_2_TYPE.get(ext, "other")


def type_exts(ftype: str) -> list[str]:
    """Type group -> extension list (with dot); aliases normalized, unknown returns []."""
    return FILE_TYPE_EXT.get(normalize_type(ftype), [])


def type_label(ftype: str) -> str:
    return FILE_TYPE_LABEL.get(normalize_type(ftype), ftype)


# ---------- Configurable classification and preview (defaults + config overrides) ----------

# Default preview policy table: type group -> {mode: builtin|external|download, template}
# external template has a {src} placeholder (replaced with direct link); empty = open directly
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
    """Type group -> preview policy; overrides (config key preview_policy) merge per group."""
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
    """Classify by file name; ext_overrides (config key type_ext_overrides) take priority."""
    ext = Path(name or "").suffix.lower()
    if ext_overrides:
        hit = ext_overrides.get(ext) or ext_overrides.get(ext.lstrip("."))
        if hit:
            return normalize_type(str(hit))
    return classify(name)


def type_exts_with_overrides(ftype: str, ext_overrides: dict | None = None) -> list[str]:
    """Extension set for a type filter = static table union config-assigned extensions.

    Uses the same source as classify_with_overrides (type_ext_overrides) so
    that classification display and type filtering agree on the same file.
    """
    exts = list(type_exts(ftype))
    target = normalize_type(ftype)
    for ext, hit in (ext_overrides or {}).items():
        if normalize_type(str(hit)) == target:
            e = str(ext).lower()
            if not e.startswith("."):
                e = f".{e}"
            if e not in exts:
                exts.append(e)
    return exts