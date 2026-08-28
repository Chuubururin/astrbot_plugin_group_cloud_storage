"""NapCatApiAdapter —— OneBotApiPort 的 NapCat 实现（模块化装配）。

能力按 NapCat API 分类拆分为底座（调用通道/限速/能力探测）与各能力混入
（ports/capabilities 一一对应）；本文件仅做聚合装配，保持既有导入路径。
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
    """聚合装配：底座 + 六类能力混入。"""
