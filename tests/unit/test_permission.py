"""PermissionService 权限矩阵单测（AC4，docs/01 §1 规则表）+ 边界补测。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.policies import PermissionService  # noqa: E402
from core.domain.enums import PermissionLevel  # noqa: E402


def make() -> PermissionService:
    return PermissionService(
        managed_groups=["g1", "g2"],
        global_admin_qqs=["999"],
    )


class TestPermissionMatrix:
    def test_managed_group_admin_ok(self):
        svc = make()
        assert svc.can_manage("10001", "admin", "g1", "g1") is True

    def test_owner_ok(self):
        svc = make()
        assert svc.can_manage("10002", "owner", "g1", "g1") is True

    def test_member_denied(self):
        svc = make()
        assert svc.can_manage("10003", "member", "g1", "g1") is False

    def test_cross_group_member_admin_denied(self):
        # A 群管理员查 B 群 → 拒绝（AC4 核心漏洞点）
        svc = make()
        assert svc.can_manage("10001", "admin", "g2", "g1") is False

    def test_cross_group_global_admin_ok(self):
        svc = make()
        assert svc.can_manage("999", "member", "g2", "g1") is True

    def test_not_managed_group_denied_even_admin(self):
        svc = make()
        assert svc.can_manage("10001", "admin", "g3", "g1") is False

    def test_empty_managed_groups_allows_all(self):
        """订正语义：白名单为空 = 放行所有群。"""
        svc = PermissionService(managed_groups=[], global_admin_qqs=["999"])
        assert svc.can_manage("10001", "admin", "g9", "g9") is True
        assert svc.is_managed("任意群") is True

    def test_level_priority(self):
        svc = make()
        assert svc.level("999", "member") is not None  # GLOBAL_ADMIN 优先于群角色
        assert svc.level("999", "member").value >= svc.level("10001", "admin").value


# ---------- 边界补测（⑦重构后的语义安全网） ----------
# 输入归一化、None、未知 role、跨群 + 白名单空、level 单调性。


@pytest.mark.parametrize(
    "managed,group,expected",
    [
        (None, "123", True),          # None 白名单 → 放行
        ([], "123", True),            # 空白名单 → 放行（用户订正语义）
        (["123"], "123", True),
        (["123"], "456", False),
        ([123], "123", True),         # int 归一化为 str
        ([" 123 "], "123", False),    # 不做 trim（str() 仅转字符串）
    ],
)
def test_is_managed_normalization(managed, group, expected):
    svc = PermissionService(managed_groups=managed)
    assert svc.is_managed(group) is expected


def test_admin_ids_normalized_from_int():
    svc = PermissionService(global_admin_qqs=[10086])
    assert svc.level("10086", "member") is PermissionLevel.GLOBAL_ADMIN
    assert svc.level(10086, "member") is PermissionLevel.GLOBAL_ADMIN


@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", PermissionLevel.GROUP_ADMIN),
        ("admin", PermissionLevel.GROUP_ADMIN),
        ("member", PermissionLevel.GROUP_MEMBER),
        ("", PermissionLevel.GROUP_MEMBER),
        ("unknown_role", PermissionLevel.GROUP_MEMBER),  # 未知角色 → 最低层
    ],
)
def test_level_role_variants(role, expected):
    svc = PermissionService()
    assert svc.level(None, role) is expected


def test_level_none_user_is_member():
    svc = PermissionService()
    assert svc.level(None, "member") is PermissionLevel.GROUP_MEMBER


def test_can_manage_empty_whitelist_cross_group_matrix():
    """空白名单（放行所有群）下的跨群矩阵。"""
    svc = PermissionService()
    # 本群：owner/admin 可，member 不可
    assert svc.can_manage("1", "owner", "g", "g") is True
    assert svc.can_manage("1", "admin", "g", "g") is True
    assert svc.can_manage("1", "member", "g", "g") is False
    # 跨群：仅全局管理员（QQ 在册）可，即使 owner 也不行
    assert svc.can_manage("1", "owner", "g1", "g2") is False
    svc2 = PermissionService(global_admin_qqs=["1"])
    assert svc2.can_manage("1", "owner", "g1", "g2") is True


def test_can_manage_whitelist_blocks_target():
    """白名单非空且目标群不在册 → 即便是全局管理员也拒绝。"""
    svc = PermissionService(managed_groups=["g1"], global_admin_qqs=["1"])
    assert svc.can_manage("1", "owner", "gX", "gX") is False


def test_level_monotonic():
    """权限层级枚举可比较（can_manage 依赖 >= 语义）。"""
    assert PermissionLevel.GLOBAL_ADMIN.value > PermissionLevel.GROUP_ADMIN.value
    assert PermissionLevel.GROUP_ADMIN.value > PermissionLevel.GROUP_MEMBER.value
