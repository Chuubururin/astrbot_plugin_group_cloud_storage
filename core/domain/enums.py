"""领域枚举（V1.0 冻结，docs/02 §3 / docs/04 §3、§5）。"""

from __future__ import annotations

from enum import Enum


class ResourceType(str, Enum):
    """资源类型。V1.0 仅 FILE；V1.1 增加 ESSENCE；V1.2 增加 ALBUM。"""

    FILE = "file"
    ALBUM = "album"  # v9：群相册条目（资源化，媒体按需实时拉取）
    ESSENCE = "essence"  # v9：精华消息（文本/图片，摘要元数据）


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
    """权限层级（docs/04 §3）。"""

    NONE = 0
    GROUP_MEMBER = 1
    GROUP_ADMIN = 2
    GLOBAL_ADMIN = 3


class CapabilityState(str, Enum):
    """OneBot 扩展 API 能力探测状态（docs/04 §5）。"""

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BROKEN = "broken"


class OneBotErrorKind(str, Enum):
    """扩展 API 失败分类（DoD #8 / docs/04 §6）。"""

    UNSUPPORTED = "unsupported"  # 实现端无此 action
    TIMEOUT = "timeout"  # 超时
    RATE_LIMITED = "rate_limited"  # 触发限频
    REMOTE_ERROR = "remote_error"  # 其他远端错误
    LOCAL_ERROR = "local_error"  # 本地参数/逻辑错误


class OneBotApiError(Exception):
    """OneBot 扩展 API 调用失败（统一出口，禁止原始异常上浮到 core）。"""

    def __init__(self, kind: OneBotErrorKind, action: str, message: str = ""):
        self.kind = kind
        self.action = action
        self.message = message
        super().__init__(f"[{kind.value}] {action}: {message}")


class BridgeTaskState(str, Enum):
    """桥接任务状态（docs/14 §2.3 archive_map.state）。

    内部统一状态，外部状态通过 normalize_task_state() 映射。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def from_external(cls, state) -> "BridgeTaskState":
        """从外部状态（OpenList API 或旧数据）映射到内部枚举。

        支持字符串和整数（OpenList API 兼容）。
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
    def is_terminal(cls, state: "BridgeTaskState") -> bool:
        """状态是否为终态（done/failed）。"""
        return state in (cls.DONE, cls.FAILED)

    @classmethod
    def is_actionable(cls, state: "BridgeTaskState") -> bool:
        """状态是否需要继续处理（pending/running/unknown）。"""
        return state in (cls.PENDING, cls.RUNNING, cls.UNKNOWN)
