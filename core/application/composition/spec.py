"""spec — composition descriptor (coded specification).

The cloud<->local whole/parts binding contract shared by all three split
paths (volumes / video segments / text chunks):
meta.composition = {
    "kind": "volumes" | "video_segments" | "text_split",
    "parts": int,                # number of parts
    "strategy": "binary" | "keyframe" | "marker",
    "total_sha256": str | None,  # whole-file hash (reassembly verification)
    "marker": str | None,        # text chunk marker prefix
}

The read side accepts legacy shapes: {"volumes": true} (volumes/video),
{"kind": "text_split"} (text).
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
    """Build the canonical descriptor (written to meta.composition)."""
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
    """Read the composition descriptor; accepts legacy meta (infers from
    legacy keys when canonical fields are absent).
    """
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
    """Whether the resource is composite (whole/parts bound) — the full shape
    check used by list semantic parsing.
    """
    return decode_composition(meta) is not None
