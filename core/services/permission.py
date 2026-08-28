"""PermissionService —— 三层权限模型（docs/01 §1、docs/04 §3，用户订正语义）。

- managed_groups：受管群白名单；**空 = 放行所有群**（订正：白名单仅用于收窄，不用于拒绝）
- global_admin_qqs：跨群管理授权 QQ（GLOBAL_ADMIN）
- "我创建的群"（role=owned，机器人为群主）天然可管理（Page 层）
- 命令处理器不自含权限判断（DoD #2），一律经本服务
"""

from __future__ import annotations

from core.domain.enums import PermissionLevel


class PermissionService:
    def __init__(self, managed_groups: list[str], global_admin_qqs: list[str]):
        self._managed = set(managed_groups or [])
        self._admins = set(global_admin_qqs or [])
        self._has_managed = bool(self._managed)

    def is_managed(self, group_id: str) -> bool:
        """目标群是否在受管范围。**白名单为空 → 放行所有群**（user 订正）。"""
        if not self._has_managed:
            return True
        return group_id in self._managed

    def level(self, user_id: str | None, role: str = "") -> PermissionLevel:
        """计算用户权限层级。role 来自 OneBot 群角色（owner/admin/member）。"""
        if user_id and user_id in self._admins:
            return PermissionLevel.GLOBAL_ADMIN
        if role in ("owner", "admin"):
            return PermissionLevel.GROUP_ADMIN
        return PermissionLevel.GROUP_MEMBER

    def can_manage(
        self,
        user_id: str | None,
        role: str,
        target_group: str,
        actual_group: str,
    ) -> bool:
        """授权判定（docs/01 §1 规则表）：
        1) 目标群受管范围校验（白名单空 → 放行）
        2) 本群（target == actual）→ 需 GROUP_ADMIN
        3) 跨群 → 需 GLOBAL_ADMIN
        """
        if not self.is_managed(target_group):
            return False
        lv = self.level(user_id, role)
        if target_group == actual_group:
            return lv.value >= PermissionLevel.GROUP_ADMIN.value
        return lv == PermissionLevel.GLOBAL_ADMIN
