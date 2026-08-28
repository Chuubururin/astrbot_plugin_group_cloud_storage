"""StoragePlanner —— 跨群存储选群策略与容量告警（docs/09 §14.2/§14.3 P3c）。

约束：每群容量约 10GB、单 QQ 号最多 300 群 —— 上传目标群自动选择，充分利用多群空间，
统一统计与告警。
"""

from __future__ import annotations

from core.domain.sync import GroupInfo
from ports.meta_store import MetaStorePort

# 告警阈值（used/total）
ALERT_YELLOW = 0.90
ALERT_RED = 0.98


class StoragePlanner:
    def __init__(self, store: MetaStorePort):
        self.store = store

    async def pick_group(
        self,
        candidates: list[GroupInfo],
        requested_bytes: int = 0,
        prefer_owned: bool = True,
    ) -> GroupInfo | None:
        """选群（按群排序顺序：owned 优先 → sort_order/group_id）。

        容量预检：余量（total-used）不足 requested_bytes 的群自动跳过；
        全部跳过时退回按余量最大的可用群（溢出切换的调用方负责逐群试）。
        """
        pool = candidates if candidates else []
        need = max(requested_bytes, 1)

        def _ordered(groups):
            return sorted(groups, key=lambda g: (g.sort_order or 0, g.group_id))

        def _usable(groups):
            return [
                g
                for g in groups
                if g.total_space <= 0 or (g.total_space - g.used_space) >= need
            ]

        primary = pool
        if prefer_owned:
            only_owned = [g for g in pool if g.role == "owned"]
            if only_owned:
                primary = only_owned
        # 优先：owned 有序池中的可用群
        hit = _usable(_ordered(primary))
        if hit:
            return hit[0]
        # owned 全部不足 → 扩展全池（溢出切换：落到 member 群）
        hit2 = _usable(_ordered(pool))
        if hit2:
            return hit2[0]
        return _ordered(pool)[0] if pool else None

    async def pick_min_group_id(self, candidates: list[GroupInfo]) -> GroupInfo | None:
        """2026-09-01 N-07：群号值最小的群（相册/精华上传缺省目标——上限未知，
        故不做容量预检，仅按群号最小选择；忽略 owned/sort_order 偏好）。"""
        pool = candidates if candidates else []
        if not pool:
            return None
        return sorted(pool, key=lambda g: g.group_id)[0]

    async def pick_min_group_for_size(
        self, candidates: list[GroupInfo], requested_bytes: int = 0
    ) -> GroupInfo | None:
        """2026-09-01 N-07：群文件上传缺省——群号值最小同时剩余空间大于待上传文件。

        候选按群号升序扫描，首个余量（total-used）≥ requested_bytes 的群即推荐；
        全部不足时退回余量最大群（溢出语义，与 pick_group 一致；调用方负责明示）。
        """
        pool = candidates if candidates else []
        if not pool:
            return None
        need = max(requested_bytes, 1)

        def _usable(groups):
            return [
                g
                for g in groups
                if g.total_space <= 0 or (g.total_space - g.used_space) >= need
            ]

        ordered = sorted(pool, key=lambda g: g.group_id)
        hit = _usable(ordered)
        if hit:
            return hit[0]
        # 全部不足 → 余量最大群（溢出语义；前端可明示余量不足）
        return max(pool, key=lambda g: (g.total_space - g.used_space), default=None)

    @staticmethod
    def capacity_state(used: int, total: int) -> str:
        """容量状态：ok / warn（≥90%）/ danger（≥98%）。"""
        if total <= 0:
            return "unknown"
        ratio = used / total
        if ratio >= ALERT_RED:
            return "danger"
        if ratio >= ALERT_YELLOW:
            return "warn"
        return "ok"

    @staticmethod
    def capacity_stats(groups: list[GroupInfo]) -> dict:
        """多群统一统计：总容量/已用/群数/告警群。"""
        total = sum(g.total_space for g in groups if g.total_space > 0)
        used = sum(g.used_space for g in groups if g.used_space > 0)
        alerts = [
            {
                "group_id": g.group_id,
                "shown_name": g.shown_name,
                "state": StoragePlanner.capacity_state(g.used_space, g.total_space),
                "pct": round((g.used_space / g.total_space) * 100, 1)
                if g.total_space
                else 0,
            }
            for g in groups
            if g.total_space > 0
            and StoragePlanner.capacity_state(g.used_space, g.total_space) != "ok"
        ]
        return {
            "groups": len(groups),
            "total_space": total,
            "used_space": used,
            "free_space": total - used,
            "alerts": alerts,
        }
