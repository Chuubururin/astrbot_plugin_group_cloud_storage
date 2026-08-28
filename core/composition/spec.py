"""spec —— 组合描述符（编码化规范，docs/13 §7）。

统一三路拆分（分卷/视频分段/文本分片）的云端↔本地零整绑定契约：
meta.composition = {
    "kind": "volumes" | "video_segments" | "text_split",
    "parts": int,                # 分片数
    "strategy": "binary" | "keyframe" | "marker",
    "total_sha256": str | None,  # 完整文件哈希（重组校验）
    "marker": str | None,        # 文本分片标记前缀
}

读侧兼容旧形态：{"volumes": true}（分卷/视频）、{"kind": "text_split"}（文本）。
"""

from __future__ import annotations

COMPOSITION_KINDS = ("volumes", "video_segments", "text_split")


def encode_composition(
    kind: str,
    parts: int,
    strategy: str,
    total_sha256: str | None = None,
    marker: str | None = None,
) -> dict:
    """生成规范描述符（写入 meta.composition）。"""
    if kind not in COMPOSITION_KINDS:
        raise ValueError(f"unknown composition kind: {kind}")
    return {
        "kind": kind,
        "parts": int(parts),
        "strategy": strategy,
        "total_sha256": total_sha256,
        "marker": marker,
    }


def decode_composition(meta: dict | None) -> dict | None:
    """读取组合描述符；兼容旧形态 meta（无规范字段时按旧键推断）。"""
    meta = meta or {}
    comp = meta.get("composition")
    if isinstance(comp, dict) and comp.get("kind") in COMPOSITION_KINDS:
        return comp
    if meta.get("volumes") is True:
        return {
            "kind": "volumes",
            "parts": meta.get("parts") or 0,
            "strategy": "binary",
            "total_sha256": meta.get("total_sha256"),
            "marker": None,
        }
    if meta.get("kind") == "text_split":
        parts = len(meta.get("parts") or [])
        return {
            "kind": "text_split",
            "parts": parts,
            "strategy": "marker",
            "total_sha256": None,
            "marker": None,
        }
    return None


def is_composite(meta: dict | None) -> bool:
    """是否为组合资源（零整绑定）——列表语义解析的完整形态判定。"""
    return decode_composition(meta) is not None
