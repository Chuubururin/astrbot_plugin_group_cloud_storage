"""Domain enums."""

from __future__ import annotations

from enum import Enum


class ResourceType(str, Enum):
    """Resource type."""

    FILE = "file"
    ALBUM = "album"  # group album entry (indexed as a resource; media fetched on demand)
    ESSENCE = "essence"  # essence message (text/image, summary metadata)


class ResourceStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class SyncKind(str, Enum):
    FULL = "full"
    EVENT = "event"
    SNAPSHOT = "snapshot"


class SyncStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PermissionLevel(Enum):
    """Permission levels."""

    NONE = 0
    GROUP_MEMBER = 1
    GROUP_ADMIN = 2
    GLOBAL_ADMIN = 3


class CapabilityState(str, Enum):
    """Capability probe states for extended OneBot APIs."""

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BROKEN = "broken"


class OneBotErrorKind(str, Enum):
    """Extended API failure categories."""

    UNSUPPORTED = "unsupported"  # action not implemented by the adapter
    TIMEOUT = "timeout"  # timeout
    RATE_LIMITED = "rate_limited"  # rate limited
    REMOTE_ERROR = "remote_error"  # other remote error
    LOCAL_ERROR = "local_error"  # local parameter/logic error


class OneBotApiError(Exception):
    """Extended OneBot API failure (unified exit; raw exceptions never reach core)."""

    def __init__(self, kind: OneBotErrorKind, action: str, message: str = ""):
        self.kind = kind
        self.action = action
        self.message = message
        super().__init__(f"[{kind.value}] {action}: {message}")


class BridgeTaskState(str, Enum):
    """Bridge task state (archive_map.state).

    Unified internal states; external states are mapped via
    normalize_task_state() and from_external().
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def from_external(cls, state) -> "BridgeTaskState":
        """Map an external state (OpenList API or legacy data) to the internal enum.

        Accepts strings and integers (OpenList API compatibility).
        """
        if isinstance(state, int):
            _INT_MAP = {0: cls.PENDING, 1: cls.RUNNING, 2: cls.DONE, 3: cls.FAILED}
            return _INT_MAP.get(state, cls.UNKNOWN)

        _STR_MAP = {
            "succeeded": cls.DONE,
            "done": cls.DONE,
            "complete": cls.DONE,
            "running": cls.RUNNING,
            "pending": cls.PENDING,
            "ready": cls.PENDING,
            "errored": cls.FAILED,
            "error": cls.FAILED,
            "failed": cls.FAILED,
            "cancelled": cls.FAILED,
            "canceled": cls.FAILED,
        }
        return _STR_MAP.get(str(state or "").strip().lower(), cls.UNKNOWN)

    @classmethod
    def is_actionable(cls, state: "BridgeTaskState") -> bool:
        """Whether the state still needs processing (pending/running/unknown)."""
        return state in (cls.PENDING, cls.RUNNING, cls.UNKNOWN)
