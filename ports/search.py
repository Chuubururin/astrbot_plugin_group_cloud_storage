"""SearchPort — full-text search protocol (FTS5 abstraction).

The application layer runs searches through this port and never touches the
FTS tables directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SearchPort(ABC):
    """Full-text search abstraction."""

    @abstractmethod
    async def fts_match(
        self, group_id: str | None, q: str, limit: int = 2000
    ) -> list[int]:
        """FTS5 match; returns the list of matching resource ids."""

    @abstractmethod
    async def rebuild_fts(self) -> None:
        """Rebuild the FTS index (call after reset/rebuild)."""
