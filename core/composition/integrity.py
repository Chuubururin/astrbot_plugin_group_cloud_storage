"""integrity —— 完整性校验（分片/整文件 SHA-256）。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """流式整文件哈希（大文件安全）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_part(data: bytes, expected_sha: str | None) -> bool:
    """单片校验：无期望哈希视为通过（旧数据兼容）。"""
    return (not expected_sha) or sha256_bytes(data) == expected_sha


def verify_total(path: str | Path, expected_sha: str | None) -> bool:
    """整文件校验：无期望哈希视为通过。"""
    return (not expected_sha) or sha256_file(path) == expected_sha
