"""SearchKV — instant search index (name prefix/substring + full-text matching).

Queries combine name prefix/substring with full-text matching over
"name + summary + tags + group name" -> row-id set -> SQL IN pagination.
Index maintenance is handled by the store layer (SQLite FTS5); see SearchKV.
"""

from __future__ import annotations


from ports.meta_store import MetaStorePort


class SearchKV:
    """Instant search facade over SQLite FTS5
    (adapters/store/sqlite.resources_fts).

    Scale target: millions of files / tens of thousands of groups — on-disk
    index, millisecond queries, zero memory footprint.
    ensure_*/mark_dirty/rebuild remain as cheap no-ops for call-site
    compatibility.
    """

    def __init__(self, store: MetaStorePort):
        self.store = store

    def add(self, row_id: int, group_id: str, name: str) -> None:
        """No-op (maintained by FTS triggers)."""

    def remove(self, row_id: int) -> None:
        """No-op (maintained by FTS triggers)."""

    def mark_dirty(self, group_id: str) -> None:
        """No-op (FTS always stays consistent with the tables)."""

    async def ensure_group(self, group_id: str) -> bool:
        return True

    async def match_ids(self, group_id: str | None, q: str) -> list[int]:
        """FTS5 search (trigram substring + LIKE fallback for short terms)."""
        return await self.store.fts_match(group_id, q)

    def stats(self) -> dict:
        return {"backend": "sqlite-fts5-trigram", "memory": 0}
