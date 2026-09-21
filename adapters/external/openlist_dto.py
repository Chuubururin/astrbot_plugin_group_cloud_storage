"""OpenList wire DTOs, constants and the task-state normalizer.

Split out of ``openlist.py`` so the client module fits the <700-line gate
(W-3 follow-up).  This module is pure data: no httpx, no I/O, no client
instance - just the shapes the control plane speaks in.

``openlist.py`` re-exports every name here, so importing from either module
works; existing call sites need no change.
"""

from __future__ import annotations

from dataclasses import dataclass

# DTOs


@dataclass(frozen=True)
class OfflineTask:
    """Offline download task representation."""

    id: str
    name: str
    state: str
    status: str
    progress: float
    error: str


@dataclass(frozen=True)
class NetFile:
    """File/directory entry from remote listing."""

    name: str
    size: int
    is_dir: bool
    modified: str
    sign: str = ""


@dataclass(frozen=True)
class DirectLink:
    """Direct URL for file access ."""

    url: str


# Hard stop for paginated listings. The server's has_more flag is trusted, so a
# server that never clears it would otherwise loop forever and accumulate
# entries without bound.
_MAX_LIST_PAGES = 1000

# OpenList reports a path outside every storage mount with these markers
# (2026-09-21 live: "failed get storage: storage not found; rawPath: ...").
_STORAGE_MARKERS = ("failed get storage", "storage not found")


def _normalize_task_state(state) -> str:
    """Normalize task state from OpenList to internal representation.

    Handles both string and integer state values from OpenList API.
    Delegates to BridgeTaskState.from_external() for single source of truth.
    """
    from core.domain.enums import BridgeTaskState

    return BridgeTaskState.from_external(state).value
