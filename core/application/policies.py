"""Application policies — three-tier permission model.

- managed_groups: managed-group whitelist; **empty = all groups allowed**
  (the whitelist only narrows the scope, it does not deny)
- global_admin_qqs: cross-group admin QQ accounts (GLOBAL_ADMIN)
- "groups created by the bot" (role=owned, bot as group owner) are manageable
  by nature (Page layer)
- Command handlers contain no permission logic of their own; everything goes
  through this service
"""

from __future__ import annotations

from core.domain.enums import PermissionLevel


class PermissionService:
    def __init__(
        self,
        managed_groups: list[str] | None = None,
        global_admin_qqs: list[str] | None = None,
    ):
        self._managed = set(str(g) for g in (managed_groups or []))
        self._admins = set(str(q) for q in (global_admin_qqs or []))
        self._has_managed = bool(self._managed)

    def is_managed(self, group_id: str) -> bool:
        """Whether the target group is within the managed scope.
        **Empty whitelist -> all groups allowed**.
        """
        if not self._has_managed:
            return True
        return group_id in self._managed

    def level(self, user_id: str | int | None, role: str = "") -> PermissionLevel:
        """Compute the user's permission level. role comes from the OneBot
        group role (owner/admin/member).

        user_id is normalized to str with the same rule as the constructor
        side (OneBot may report the QQ id as int)."""
        uid = str(user_id) if user_id is not None else None
        if uid and uid in self._admins:
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
        """Authorization check:
        1) Target-group managed-scope validation (empty whitelist -> allowed)
        2) Same group (target == actual) -> requires GROUP_ADMIN
        3) Cross-group -> requires GLOBAL_ADMIN
        """
        if not self.is_managed(target_group):
            return False
        lv = self.level(user_id, role)
        if target_group == actual_group:
            return lv.value >= PermissionLevel.GROUP_ADMIN.value
        return lv == PermissionLevel.GLOBAL_ADMIN
