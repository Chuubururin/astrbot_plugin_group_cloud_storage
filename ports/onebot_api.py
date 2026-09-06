"""OneBotApiPort — the only exit point from the business layer to the OneBot adapter.

- core/services and commands must not call `event.bot.call_action` directly
- adapters are responsible for JSON -> DTO conversion
- capabilities are modularized by NapCat API category in ports/capabilities.py;
  this interface aggregates all capability protocols plus probing and lifecycle
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.domain.enums import CapabilityState
from ports.capabilities import (
    AlbumCapability,
    CoreCapability,
    FileCapability,
    GoCqFileCapability,
    GroupCapability,
    GroupExtendsCapability,
)


class OneBotApiPort(
    CoreCapability,
    GroupCapability,
    GroupExtendsCapability,
    FileCapability,
    GoCqFileCapability,
    AlbumCapability,
    ABC,
):
    """OneBot adapter API abstraction (capability protocol aggregation + probing + lifecycle)."""

    @abstractmethod
    def capability(self, action: str) -> CapabilityState:
        """Capability state for an extended API."""

    @abstractmethod
    async def close(self) -> None: ...
