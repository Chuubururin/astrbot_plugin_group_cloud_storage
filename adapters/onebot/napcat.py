"""NapCatApiAdapter -- NapCat implementation of OneBotApiPort (modular assembly).

Capabilities are split by NapCat API category into a base class (call
channel / rate limiting / capability probing) and per-capability mixins
(one-to-one with ports/capabilities). This file only performs the
aggregation and preserves the existing import path.
"""

from __future__ import annotations

from adapters.onebot.base import NapCatBase
from adapters.onebot.mixins import (
    NapCatAlbumMixin,
    NapCatCoreMixin,
    NapCatFileMixin,
    NapCatGoCqFileMixin,
    NapCatGroupExtendsMixin,
    NapCatGroupMixin,
)
from ports.onebot_api import OneBotApiPort


class NapCatApiAdapter(
    NapCatCoreMixin,
    NapCatGroupMixin,
    NapCatGroupExtendsMixin,
    NapCatFileMixin,
    NapCatGoCqFileMixin,
    NapCatAlbumMixin,
    NapCatBase,
    OneBotApiPort,
):
    """Aggregated assembly: base class plus the six capability mixins."""
