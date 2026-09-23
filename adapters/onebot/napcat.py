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
    NapCatCoreMixin,        # MRO leftmost: call channel / rate limiting
    NapCatGroupMixin,
    NapCatGroupExtendsMixin,
    NapCatFileMixin,
    NapCatGoCqFileMixin,
    NapCatAlbumMixin,
    NapCatBase,             # base class (shared state, capability probing)
    OneBotApiPort,          # ABC rightmost: interface contract
):
    """Aggregated assembly: base class plus the six capability mixins.

    MRO resolution order (left = higher priority on method conflict):
    core > group > group-extends > file > gocq-file > album > base > port.
    """
