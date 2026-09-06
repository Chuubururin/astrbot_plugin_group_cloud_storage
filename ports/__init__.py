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
from .meta_store import MetaStorePort
from .onebot_api import OneBotApiPort
from .scheduler import SchedulerPort
from .search import SearchPort

__all__ = [
    "AlbumCapability",
    "CoreCapability",
    "FileCapability",
    "GoCqFileCapability",
    "GroupCapability",
    "GroupExtendsCapability",
    "MetaStorePort",
    "NullLimiter",
    "OneBotApiPort",
    "RateLimiter",
    "SchedulerPort",
    "SearchPort",
]
