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
# defaults below. Reconciliation kinds (sync/file_scan) are deliberately NOT
# here: a full-group scan wave submits hundreds of tasks ahead of everything
# already in the FIFO hi queue and starved interactive ops (live 2026-09-12:
# deletes queued 30+ min behind a file_scan wave; replace_name queued behind
# a 292-task scan wave for ~1h — missed in the first pass, added same day).
# They run on the normal worker pool, leaving hi workers free for
# user-initiated ops.
DEFAULT_HIGH_PRIORITY = {
    "rename",
    "move_file",
    "replace_name",
    "upload",
    "delete",
    "essence_save",
    "essence_delete",
    "fetch",
    "video_upload",
    "video_album",
    "image_album",
    "bridge_out",
    "bridge_in",
    "convert_volumes",
    "batch_groups",
    "create_folder",
}

__all__ = ["Op", "OpCancelError", "OpPausedError", "BULK_KINDS", "DEFAULT_HIGH_PRIORITY"]
