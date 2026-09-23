"""ports — stable persistence and external protocol layer (explicit port set).

Port contracts (package purity is enforced by the TestPortsPurity architecture
test and the matching import-linter contract):
- the application layer reaches the outside world only through ports;
  importing adapters.* is forbidden
- every Protocol in each port module must be exported via __all__ explicitly
  (TestPortsExplicitExports)
"""
from .capabilities import (
    AlbumCapability,
    CoreCapability,
    FileCapability,
    GoCqFileCapability,
    GroupCapability,
    GroupExtendsCapability,
)
from .limiter import NullLimiter, RateLimiter
from .meta_store import (
    ActivityPort,
    ArchivePort,
    FolderPort,
    GroupPort,
    LedgerPort,
    MetaStorePort,
    NetdiskIndexPort,
    ResourceReadPort,
    ResourceWritePort,
    StoreAdminPort,
    VolumePort,
)
from .onebot_api import OneBotApiPort

__all__ = [
    "ActivityPort",
    "AlbumCapability",
    "ArchivePort",
    "CoreCapability",
    "FileCapability",
    "FolderPort",
    "GoCqFileCapability",
    "GroupCapability",
    "GroupExtendsCapability",
    "GroupPort",
    "LedgerPort",
    "MetaStorePort",
    "NetdiskIndexPort",
    "NullLimiter",
    "OneBotApiPort",
    "RateLimiter",
    "ResourceReadPort",
    "ResourceWritePort",
    "StoreAdminPort",
    "VolumePort",
]
