"""core/domain — pure domain models (value objects / enums / domain exceptions),
with zero application-layer dependencies.

Explicit export set: register new domain models here (same discipline as
ports.__init__).
"""
from .enums import (
    BridgeTaskState,
    CapabilityState,
    OneBotApiError,
    OneBotErrorKind,
    PermissionLevel,
    ResourceStatus,
    ResourceType,
    SyncKind,
    SyncStatus,
)
from .resource import GroupFolder, Resource
from .sync import (
    GroupInfo,
    Page,
    PageItem,
    ResourceQuery,
    ResourceStats,
    ScanResult,
    Snapshot,
    SyncLog,
    SyncResult,
    VolumeInfo,
)

__all__ = [
    "BridgeTaskState",
    "CapabilityState",
    "GroupFolder",
    "GroupInfo",
    "OneBotApiError",
    "OneBotErrorKind",
    "Page",
    "PageItem",
    "PermissionLevel",
    "Resource",
    "ResourceQuery",
    "ResourceStats",
    "ResourceStatus",
    "ResourceType",
    "ScanResult",
    "Snapshot",
    "SyncKind",
    "SyncLog",
    "SyncResult",
    "SyncStatus",
    "VolumeInfo",
]
