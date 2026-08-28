"""OneBotApiPort —— 业务层对 OneBot 实现端的唯一出口（docs/02 §3、docs/04 §2）。

- core/services 与 commands 禁止直接使用 `event.bot.call_action`（DoD #1）
- 适配器负责 JSON → DTO 转换（DoD #3）
- 能力按 NapCat API 分类模块化定义于 ports/capabilities.py，
  本接口聚合全部能力协议 + 能力探测 + 生命周期
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
    """OneBot 实现端 API 抽象（能力协议聚合 + 探测 + 生命周期）。"""

    @abstractmethod
    def capability(self, action: str) -> CapabilityState:
        """扩展 API 能力状态（docs/04 §5）。"""

    @abstractmethod
    async def close(self) -> None: ...
