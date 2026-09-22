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


def build_method_map(parts) -> dict[str, str]:
    """Static {forwarded name: part attr} map for the composed store.

    Built once at import. Two parts defining the same public name would
    otherwise be resolved by declaration order alone -- the later definition
    would simply never run. That silent shadowing is a hard error here.
    """
    owner: dict[str, str] = {}
    for attr, part_cls in parts:
        for name in dir(part_cls):
            if name.startswith("_"):
                continue
            prev = owner.get(name)
            if prev is not None and prev != attr:
                raise RuntimeError(
                    f"duplicate forwarded method {name!r}: {prev} and {attr} both define it"
                )
            owner[name] = attr
    return owner


class StorePart:
    """Mixin base class: delegates `self._conn` / `self._db_path` /
    `self._tag_cloud_cache` to SharedState so mixin method bodies work
    unchanged under composition.

    Cross-part method calls (e.g. one resource part calling
    ``upsert_resources`` on another) are forwarded through `self._owner` to
    the aggregator's public interface; a missing name raises AttributeError.
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
        # resources_write.py invalidates via `self._tag_cloud_cache = {}`
        self._state.tag_cloud_cache = value

    def __getattr__(self, name: str) -> Any:
        """Methods missing on this part are forwarded to the aggregator's
        public interface (cross-part calls).

        Resolution goes through the aggregator's static name -> part map, so a
        name claimed by two parts fails at import time instead of being
        silently resolved by declaration order.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        owner = self._state.owner
        if owner is None:
            raise AttributeError(name)
        # owner 恒为 SqliteMetaStore（SharedState/owner 只在 store.py 构造）——
        # 用 getattr 兜底：owner 是别的类型时退化成 AttributeError 而不是炸掉。
        method_owner = getattr(owner, "_METHOD_OWNER", None)
        if method_owner is None:
            raise AttributeError(name)
        attr = method_owner.get(name)
        if attr is None:
            raise AttributeError(name)
        part = owner.__dict__.get(attr)
        if part is None:
            raise AttributeError(name)
        return getattr(part, name)
