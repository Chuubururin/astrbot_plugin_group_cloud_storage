"""Op model and queue-level exceptions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Op:
    """A pending external operation."""

    task_id: str
    kind: str  # scan | rename | upload | delete | move | sync
    target: str = ""  # group_id
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    retries: int = 0
    cancel: bool = False
    pause: bool = False  # cooperative pause (takes effect at handler checkpoints)
    error: str | None = None
    account: str | None = None  # account key (rate-limit/concurrency scope)


class OpCancelError(Exception):
    """The operation was cancelled."""


class OpPausedError(Exception):
    """The operation was paused (cooperative: raised by the handler at a
    checkpoint; the worker holds it until resume)."""


# Non-interactive bulk operations (mostly local computation; API sub-calls are
# rate-limited per account by the adapter) -- concurrency-capped, not
# rate-limited
BULK_KINDS = {
    "convert_volumes",
    "video_upload",
    "video_album",
    "fetch",
    "netdisk_index",
}

# The priority set is configurable (config.op_high_priority_kinds); built-in
# defaults below
DEFAULT_HIGH_PRIORITY = {
    "rename",
    "move_file",
    "upload",
    "delete",
    "sync",
    "file_scan",
    "essence_save",
    "essence_delete",
    "fetch",
    "video_upload",
    "video_album",
    "convert_volumes",
    "batch_groups",
    "create_folder",
}

__all__ = ["Op", "OpCancelError", "OpPausedError", "BULK_KINDS", "DEFAULT_HIGH_PRIORITY"]
