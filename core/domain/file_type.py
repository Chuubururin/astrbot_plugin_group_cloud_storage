"""File type dictionary: 13-category classification.

- `classify(name)` -> group name (unknown -> other); strips trailing
  transient/volume suffixes (download intermediates, ".001" splits) and
  retries the table, so external-tool names like "x.rar.netdisk.p.downloading"
  classify as archive
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

import re
from pathlib import Path

FILE_TYPE_EXT: dict[str, list[str]] = {
    "document": [
        ".doc", ".docx", ".docm", ".odt", ".rtf", ".wps", ".txt", ".md",
        ".epub", ".mobi", ".azw3", ".chm", ".caj", ".tex",
    ],
    "pdf": [".pdf"],
    "spreadsheet": [".xls", ".xlsx", ".xlsb", ".xlsm", ".et", ".csv", ".ods", ".numbers"],
    "slide": [".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".dps", ".odp", ".key"],
    "online_doc": [],  # no fixed extensions; extensible via the type_ext_overrides config
    "image": [
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic",
        ".tif", ".tiff", ".ico", ".avif", ".jfif", ".apng", ".psd", ".ai", ".eps", ".dng",
    ],
    "video": [
        ".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv",
        ".m4v", ".ts", ".m2ts", ".mts", ".3gp", ".3g2", ".ogv", ".rmvb", ".rm",
        ".vob", ".mpg", ".mpeg", ".f4v", ".asf", ".divx",
        ".m3u", ".m3u8",  # playlists (IPTV sources)
    ],
    "audio": [
        ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac",
        ".ape", ".wma", ".opus", ".mid", ".midi", ".amr",
        ".aiff", ".aif", ".ac3", ".mka", ".tak", ".m4b",
    ],
    "archive": [
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
        ".tgz", ".tbz2", ".txz", ".cab", ".iso", ".jar",
        ".arj", ".lzh", ".zst", ".lz4", ".lzma",
    ],
    "installer": [
        ".exe", ".msi", ".apk", ".deb", ".rpm", ".dmg", ".appimage",
        ".appx", ".msix", ".ipa", ".xapk", ".apkm", ".pkg", ".flatpak", ".snap", ".whl",
    ],
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


# Transient download-intermediate suffixes produced by external tools
# (e.g. SnowLuma names a partial netdisk download "x.rar.netdisk.p.downloading"),
# and split-volume numeric suffixes (".001".." / .z01" / ".r00" style). When the
# last suffix is one of these, classify strips it and retries the table, so
# "x.rar.netdisk.p.downloading" classifies as archive instead of other. The
# strip loop is strictly additive: it only runs while the current suffix is NOT
# already a known type, so every previously classified file keeps its type.
_TRANSIENT_EXTS: frozenset[str] = frozenset(
    {".downloading", ".crdownload", ".download", ".partial", ".part",
     ".tmp", ".temp", ".p", ".netdisk"}
)
_VOLUME_EXT_RE = re.compile(r"^\.(?:\d{2,4}|[zrs]\d{1,3})$")


def classify(name: str) -> str:
    """Return the type group for a file name (falls back to other)."""
    p = Path(name or "")
    ext = p.suffix.lower()
    for _ in range(6):
        if ext in _EXT_2_TYPE:
            return _EXT_2_TYPE[ext]
        if ext not in _TRANSIENT_EXTS and not _VOLUME_EXT_RE.match(ext):
            break
        p = p.with_suffix("")
        ext = p.suffix.lower()
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
        # BUG-27 fix: str(...).split(",") always returns non-empty list
        # ("".split(",") == [""]), so the original condition was always True.
        # Now explicitly filter empty strings before checking membership.
        types_str = str(ov.get("types", "") or "")
        # Normalize both sides before matching: the raw ftype never matches a
        # historical alias, so a documented config like {"types": "document,data"}
        # could not override the "other" group (data -> other) and
        # {"types": "program"} could not override "installer". Same convention
        # as classify_with_overrides / type_exts.
        type_list = [
            normalize_type(t.strip()) for t in types_str.split(",") if t.strip()
        ]
        if type_list and normalize_type(ftype) in type_list:
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
