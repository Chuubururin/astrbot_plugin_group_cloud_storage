"""Shared persistence state for composed store parts.

SqliteMetaStore is composed of independent domain mix-in instances
(ResourcesMixin and peers). Each part accesses shared state (connection
manager, database path, tag cloud cache) through StorePart delegation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connection import ConnectionManager


@dataclass
class SharedState:
    """Mutable state shared across store parts (the connection can be
    replaced in place by reset_and_rebuild)."""

    conn: ConnectionManager
    db_path: Path
    tag_cloud_cache: dict = field(default_factory=dict)
    owner: Any = None  # aggregator (SqliteMetaStore), used for cross-part calls


class StorePart:
    """Mixin base class: delegates `self._conn` / `self._db_path` /
    `self._tag_cloud_cache` to SharedState so mixin method bodies work
    unchanged under composition.

    Cross-part method calls (e.g. folders calling resources.upsert_resources)
    are forwarded through `self._owner` to the aggregator's public
    interface; a missing name raises AttributeError.
    """

    def __init__(self, state: SharedState) -> None:
        self._state = state

    @property
    def _owner(self):
        return self._state.owner

    @property
    def _conn(self) -> ConnectionManager:
        return self._state.conn

    @property
    def _db_path(self) -> Path:
        return self._state.db_path

    @property
    def _tag_cloud_cache(self) -> dict:
        return self._state.tag_cloud_cache

    @_tag_cloud_cache.setter
    def _tag_cloud_cache(self, value: Any) -> None:
        # resources.py invalidates via `self._tag_cloud_cache = {}`
        self._state.tag_cloud_cache = value

    def __getattr__(self, name: str):
        # Methods missing on this part are forwarded to the aggregator's
        # public interface (cross-part calls).
        # Only class-level existence checks are performed (the instance
        # __getattr__ chain is not triggered) to prevent recursion.
        owner = self._state.owner
        if owner is not None and not name.startswith("_"):
            for _attr, part in owner.iter_parts():
                if name in vars(type(part)) or hasattr(type(part), name):
                    return getattr(part, name)
        raise AttributeError(name)
