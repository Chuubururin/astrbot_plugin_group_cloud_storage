"""Derived identity columns for a resource row.

``resource_id``, ``logical_key`` and ``path`` are all *derived* -- nothing
here touches the database. Kept apart from ``resources.py`` so the identity
rules are readable in one place (and so that module stays under its size
gate). ``logical_key`` is the stable one: ``resource_id`` embeds a
session-scoped NapCat file_id and changes on every restart.
"""
from __future__ import annotations

def _logical_key(r) -> str:
    """Stable identity of a logical file: ``group:type:name``.

    Deliberately independent of ``source_ref`` -- that is a session-scoped
    NapCat handle which changes on every restart, and binding identity to it
    is what produced duplicate rows for one file. ``name`` is part of the
    identity because a group's cloud listing is a flat namespace per type.
    """
    t = r.type.value if hasattr(r.type, "value") else str(r.type)
    return f"{r.group_id}:{t}:{r.name}"


_PATH_SPECIAL = {"album": "__album__", "essence": "__essence__"}


def _path_for(group_id: str, t: str, name: str, folder_name: str | None) -> str:
    """Canonical ``path`` for one row -- shared by upsert and rename."""
    if t in _PATH_SPECIAL:
        return f"/{group_id}/{_PATH_SPECIAL[t]}/{name}"
    if t == "file" and folder_name:
        return f"/{group_id}/{folder_name}/{name}"
    return f"/{group_id}/{name}"


def _logical_path(r) -> tuple[str, str]:
    t = r.type.value if hasattr(r.type, "value") else str(r.type)
    if t == "file":
        dot = r.name.rfind(".")
        ext = r.name[dot + 1 :].lower() if dot > 0 else ""
    else:
        ext = t if t in _PATH_SPECIAL else ""
    return _path_for(r.group_id, t, r.name, r.folder_name), ext
