"""GroupScanService —— 启动扫描"我创建的群"（docs/09 §12.4）。

- 判定：get_group_list → 逐群 get_group_member_list，群主（role=owner）== 机器人自身 → owned
- 复合操作在 OpQueue 内执行：逐群调用复用共享限速器（外部每个调用的字节间隔 ≥ interval）
- 幂等：group_id 唯一键 upsert（role/group_name/last_scan_at 更新，display_name/label/sort_order 保留）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

from core.domain.sync import GroupInfo
from core.log import logger
from core.services.op_queue import OpQueue
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort


@dataclass
class ScanResult:
    total: int = 0
    owned: int = 0
    scanned_at: int = 0
    failed: int = 0  # 本次扫描中 API 失败的群数

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "owned": self.owned,
            "scanned_at": self.scanned_at,
            "failed": self.failed,
        }


class GroupScanService:
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        auto_label: bool = True,
        on_account_resolved: Callable[[object, str], Awaitable[None]] | None = None,
    ):
        self.api = api
        self.store = store
        self.queue = queue
        self.auto_label = auto_label
        self.last_result: ScanResult | None = None
        # 扫描成功后回调：(bot, account_id) → 注册映射 + 恢复 managed
        self._on_account_resolved = on_account_resolved

    async def _with_timeout(self, coro, timeout: float = 15.0):
        """为外部调用加超时：单群 API 挂起不拖死整轮扫描（超时按异常抛出）。"""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"call timeout after {timeout}s")

    async def _capacity_of(self, group_id: str, api=None) -> tuple[int, int, int, int] | None:
        """统一容量口径（云端优先 → 本地索引兜底）。

        fs 成功且 total>0 → 返回四元组。
        fs 失败或 total=0 → 返回 None（调用方跳过容量写入，保留上次值）。
        """
        _api = api or self.api
        try:
            fs = await _api.get_group_fs_info(group_id)
        except Exception:
            return None
        if not fs.total_space:
            return None
        used = fs.used_space or await self.store.sum_resource_sizes(group_id)
        count = fs.file_count or await self.store.count_active(group_id)
        return used, fs.total_space, count, fs.limit_count

    async def scan_owned(
        self,
        include_capacity: bool = True,
        force_role_scan: bool = False,
        account_bot=None,
        api_override=None,
        group_filter: list[str] | None = None,
    ) -> ScanResult:
        """扫描群信息（调用方负责放入 OpQueue；逐群调用自行限速）。

        group_filter: 哈希分片后每 bot 只扫自己分片的群（None=全部）。

        效率策略（300 群场景，docs/09 §13.3）：
        - owned 判定 = 群列表 diff 增量：仅对**新群**调用 `get_group_member_info(self)`
          （单成员自查询，轻量）；已缓存群保留 role，不再逐群拉全量成员列表
        - 容量采集 = 全量（`include_capacity=True` 时，后台渐进更新，供跨群统计）
        - 进度经 queue.publish 推送（i/N），Page SSE 可见

        api_override: v2.12 并行扫描——传入独立 adapter 实例，避免全局状态竞争。
        """
        api = api_override or self.api
        if account_bot is not None and hasattr(api, "with_bot"):
            api.with_bot(account_bot)
        me = (await api.get_login_info() or {}).get("user_id")
        me = str(me or "")
        # 登记 bot → account_id 映射 + 恢复该账号群为 managed=1
        if me and self._on_account_resolved:
            try:
                await self._on_account_resolved(account_bot, me)
            except Exception as e:
                logger.debug(f"[group-scan] on_account_resolved callback failed: {e}")
        groups = await api.list_groups()
        # 哈希分片过滤：分片内的群 + DB 未知的新群（发现关键路径）一律放行
        if group_filter is not None:
            filter_set = set(group_filter)
            known_ids = {g.group_id for g in await self.store.list_groups()}
            groups = [
                g for g in groups
                if str(g.get("group_id") or "") in filter_set
                or str(g.get("group_id") or "") not in known_ids
            ]
        group_total = len(groups)
        # 增量判定：读取已有 role 缓存
        known = {g.group_id: g for g in await self.store.list_groups()}
        owned = 0
        failed = 0  # API 失败群数（熔断器信号）
        now = int(time.time())
        judged = 0  # 本次实际判定的新群数
        last_pub = 0.0
        for i, g in enumerate(groups, 1):
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            prev = known.get(gid)
            role = prev.role if prev else "unknown"
            # 新群 → 轻量自查询判定 owned（每个新群一次 API）
            if role in ("unknown",) or force_role_scan:
                try:
                    await self.queue.acquire()
                    me_info = await self._with_timeout(api.get_group_member_info(gid, me))
                    role = "owned" if me_info.get("role") == "owner" else "member"
                    judged += 1
                except Exception as e:
                    logger.warning(f"[group-scan] role judge failed for {gid}: {e}")
                    role = prev.role if prev else "unknown"
            # 容量采集：fs 失败返回 None → 保留 prev 值，不写 0 覆盖
            cap_used = cap_total = cap_count = cap_limit = 0
            cap_ok = False
            album_c = essence_c = 0
            if include_capacity:
                try:
                    await self.queue.acquire()
                    _cap = await self._with_timeout(
                        self._capacity_of(gid, api)
                    )
                    if _cap is not None:
                        cap_used, cap_total, cap_count, cap_limit = _cap
                        cap_ok = True
                except Exception as e:
                    logger.debug(f"[group-scan] fs_info unavailable for {gid}: {e}")
            if not cap_ok and prev:
                cap_used = prev.used_space
                cap_total = prev.total_space
                cap_count = prev.file_count
                cap_limit = prev.limit_count
            # v8/v9：相册/精华采集 + 资源化入库（全量节奏，每群都采集）
            if include_capacity:
                try:
                    await self.queue.acquire()
                    albums_raw = await self._with_timeout(api.get_qun_album_list(gid))
                    album_c = len(albums_raw)
                    await self.queue.acquire()
                    essences_raw = await self._with_timeout(api.get_essence_msg_list(gid))
                    essence_c = len(essences_raw)
                    await self.store.upsert_album_essence(gid, albums_raw, essences_raw)
                except Exception as e:
                    logger.debug(
                        f"[group-scan] album/essence unavailable for {gid}: {e}"
                    )
            if role == "owned":
                owned += 1
            # 边扫边落库：极大数量下列表随扫描即时可见，而非扫描结束一次性写入
            await self.store.upsert_groups(
                [
                    GroupInfo(
                        group_id=gid,
                        group_name=str(g.get("group_name") or ""),
                        role=role,
                        last_scan_at=now,
                        used_space=cap_used,
                        total_space=cap_total,
                        file_count=cap_count,
                        limit_count=cap_limit,
                        album_count=album_c,
                        essence_count=essence_c,
                        account_id=me,
                    )
                ]
            )
            if i % 10 == 0 or i == group_total or time.monotonic() - last_pub >= 5.0:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                        "judged": judged,
                    }
                )
                # 扫描中动态刷新：前端按防抖重新拉取部分数据（边扫边加载）
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                    }
                )
                last_pub = time.monotonic()
        self.last_result = ScanResult(total=group_total, owned=owned, scanned_at=now, failed=failed)
        logger.info(
            f"[group-scan] done: total={group_total} owned={owned} "
            f"failed={failed} judged_new={judged} (ts={now})"
        )
        if self.auto_label:
            await self.auto_fill_labels()
        return self.last_result

    async def scan_groups(self, group_ids: list[str]) -> ScanResult:
        """范围扫描：仅对指定群做容量采集 + 新群 owned 判定（复用扫描限速）。"""
        me = (await self.api.get_login_info() or {}).get("user_id")
        me = str(me or "")
        known = {g.group_id: g for g in await self.store.list_groups()}
        now = int(time.time())
        owned = 0
        total = len(group_ids)
        last_pub = 0.0
        for i, gid in enumerate(group_ids, 1):
            prev = known.get(gid)
            role = prev.role if prev else "unknown"
            if role in ("unknown",):
                try:
                    await self.queue.acquire(mult=2.0)
                    me_info = await self._with_timeout(
                        self.api.get_group_member_info(gid, me)
                    )
                    role = "owned" if me_info.get("role") == "owner" else "member"
                except Exception as e:
                    logger.warning(f"[group-scan] role judge failed for {gid}: {e}")
                    role = prev.role if prev else "unknown"
            cap_used = cap_total = cap_count = cap_limit = 0
            cap_ok = False
            try:
                await self.queue.acquire(mult=2.0)
                _cap = await self._with_timeout(
                    self._capacity_of(gid)
                )
                if _cap is not None:
                    cap_used, cap_total, cap_count, cap_limit = _cap
                    cap_ok = True
            except Exception as e:
                logger.debug(f"[group-scan] fs_info unavailable for {gid}: {e}")
            if not cap_ok and prev:
                cap_used = prev.used_space
                cap_total = prev.total_space
                cap_count = prev.file_count
                cap_limit = prev.limit_count
            if role == "owned":
                owned += 1
            # 边扫边落库：大数量下列表随扫描即时可见
            await self.store.upsert_groups(
                [
                    GroupInfo(
                        group_id=gid,
                        group_name=(prev.group_name if prev else ""),
                        role=role,
                        last_scan_at=now,
                        used_space=cap_used,
                        total_space=cap_total,
                        file_count=cap_count,
                        limit_count=cap_limit,
                    )
                ]
            )
            if (
                i % 10 == 0 or i == total or time.monotonic() - last_pub >= 5.0
            ):  # §18.3 进度节流
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "judged": 0,
                    }
                )
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": total,
                    }
                )
                last_pub = time.monotonic()
        self.last_result = ScanResult(total=total, owned=owned, scanned_at=now)
        logger.info(f"[group-scan] range scan done: {total} groups owned={owned}")
        return self.last_result

    async def default_range_ids(self) -> list[str]:
        """默认范围：排序序列中第一个「已用容量为 -」（无容量数据）的群，
        及其上方（排序更前）的 2 个群；不足则取全部可用（容量未知优先）。"""
        groups = [g for g in await self.store.list_groups() if getattr(g, "managed", 1)]
        ordered = sorted(groups, key=lambda g: (g.sort_order or 0, g.group_id))
        # 容量未知 = 无 total 或 used 为 0 且未扫描（last_scan_at 缺失）
        target_i = next(
            (
                i
                for i, g in enumerate(ordered)
                if g.total_space <= 0 or (g.used_space <= 0 and not g.last_scan_at)
            ),
            None,
        )
        if target_i is None:
            return []
        start = max(0, target_i - 2)
        return [g.group_id for g in ordered[start : target_i + 1]]

    async def run_batch_ops(self, op) -> None:
        """批量群操作（v1.4）：改名/加群方式/备注 —— 逐群真实调用 + 本地回填。"""
        action = op.payload.get("action")
        value = op.payload.get("value")
        group_ids = list(op.payload.get("group_ids") or [])
        total = len(group_ids)
        if action == "rename":
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_name(gid, str(value))
                await self.store.update_group_fields(gid, group_name=str(value))
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"改名 {gid}",
                    }
                )
        elif action == "add_option":
            add_type = int(value)
            if add_type not in (1, 2, 3, 4, 5):
                raise ValueError("add_type must be 1..5")
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_add_option(gid, add_type)
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"加群方式 {gid}",
                    }
                )
        elif action == "remark":
            for i, gid in enumerate(group_ids, 1):
                await self.queue.acquire()
                await self.api.set_group_remark(gid, str(value))
                await self.store.update_group_fields(gid, display_name=str(value))
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "batch_groups",
                        "target": "*",
                        "i": i,
                        "n": total,
                        "detail": f"备注 {gid}",
                    }
                )
        else:
            raise ValueError(f"unknown batch action: {action}")
        logger.info(f"[group-scan] batch {action} done: {total} groups")

    async def scan_owned_incremental(
        self, account_bot=None, api_override=None,
        group_filter: list[str] | None = None,
    ) -> ScanResult:
        """增量群信息同步（默认节奏）：
        - 仅对「新群/容量未知群」采集容量 + 判定 owned
        - 存量已知群：仅更新 group_name/last_scan_at（不拉容量）

        group_filter: 哈希分片后每 bot 只扫自己分片的群（None=全部）。
        """
        api = api_override or self.api
        if account_bot is not None and hasattr(api, "with_bot"):
            api.with_bot(account_bot)
        me = (await api.get_login_info() or {}).get("user_id")
        me = str(me or "")
        # 登记 bot → account_id 映射 + 恢复该账号群为 managed=1
        if me and self._on_account_resolved:
            try:
                await self._on_account_resolved(account_bot, me)
            except Exception as e:
                logger.debug(f"[group-scan] on_account_resolved callback failed: {e}")
        groups = await api.list_groups()
        known = {g.group_id: g for g in await self.store.list_groups()}
        # 哈希分片过滤：分片内的群 + DB 未知的新群（发现关键路径）一律放行
        if group_filter is not None:
            filter_set = set(group_filter)
            known_ids = set(known.keys())
            groups = [
                g for g in groups
                if str(g.get("group_id") or "") in filter_set
                or str(g.get("group_id") or "") not in known_ids
            ]
        group_total = len(groups)
        now = int(time.time())
        owned = 0
        failed = 0  # API 失败群数（熔断器信号）
        judged = 0
        last_pub = 0.0
        for i, g in enumerate(groups, 1):
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            prev = known.get(gid)
            # 新群 或 容量未知 → 判定+容量；存量已知 → 仅名称/时间戳
            need = prev is None or prev.total_space <= 0 or not prev.last_scan_at
            role = prev.role if prev else "unknown"
            album_c = essence_c = 0
            cap_used = cap_total = cap_count = cap_limit = 0
            cap_ok = False
            if need:
                if role in ("unknown",):
                    try:
                        await self.queue.acquire(mult=2.0)
                        me_info = await self._with_timeout(
                            api.get_group_member_info(gid, me)
                        )
                        role = "owned" if me_info.get("role") == "owner" else "member"
                        judged += 1
                    except Exception as e:
                        logger.warning(f"[group-scan] role judge failed for {gid}: {e}")
                        role = prev.role if prev else "unknown"
                        failed += 1
                try:
                    await self.queue.acquire(mult=2.0)
                    _cap = await self._with_timeout(
                        self._capacity_of(gid, api)
                    )
                    if _cap is not None:
                        cap_used, cap_total, cap_count, cap_limit = _cap
                        cap_ok = True
                except Exception as e:
                    logger.debug(f"[group-scan] fs_info unavailable for {gid}: {e}")
                if not cap_ok and prev:
                    cap_used = prev.used_space
                    cap_total = prev.total_space
                    cap_count = prev.file_count
                    cap_limit = prev.limit_count
                # v8/v9 资源统计：相册/精华（仅新群/未知群采集 + 资源化入库）
                try:
                    await self.queue.acquire(mult=2.0)
                    albums_raw = await self._with_timeout(api.get_qun_album_list(gid))
                    album_c = len(albums_raw)
                    await self.queue.acquire(mult=2.0)
                    essences_raw = await self._with_timeout(api.get_essence_msg_list(gid))
                    essence_c = len(essences_raw)
                    await self.store.upsert_album_essence(gid, albums_raw, essences_raw)
                except Exception as e:
                    logger.debug(
                        f"[group-scan] album/essence unavailable for {gid}: {e}"
                    )
            else:
                # 存量已知群不拉云端容量；保留 prev 值
                cap_used = prev.used_space
                cap_total = prev.total_space
                cap_count = prev.file_count
                cap_limit = prev.limit_count
            if role == "owned":
                owned += 1
            # 边扫边落库：大数量下列表随扫描即时可见
            await self.store.upsert_groups(
                [
                    GroupInfo(
                        group_id=gid,
                        group_name=str(g.get("group_name") or ""),
                        role=role,
                        last_scan_at=now,
                        used_space=cap_used,
                        total_space=cap_total,
                        file_count=cap_count,
                        album_count=album_c if need else prev.album_count,
                        essence_count=essence_c if need else prev.essence_count,
                        account_id=me,
                    )
                ]
            )
            if i % 10 == 0 or i == group_total or time.monotonic() - last_pub >= 5.0:
                self.queue.publish(
                    {
                        "type": "progress",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                        "judged": judged,
                    }
                )
                self.queue.publish(
                    {
                        "type": "data_changed",
                        "kind": "scan",
                        "target": "*",
                        "i": i,
                        "n": group_total,
                    }
                )
                last_pub = time.monotonic()
        self.last_result = ScanResult(total=group_total, owned=owned, scanned_at=now, failed=failed)
        logger.info(
            f"[group-scan] incremental done: total={group_total} "
            f"owned={owned} failed={failed} touched={judged}"
        )
        return self.last_result

    async def auto_fill_labels(self) -> int:
        """自动标号（扫描后自动执行）：参考 Windows 重复文件续号 —— 已有标号保留，
        未标号群按当前顺序续填下一个未占用编号（A/B/C… 或 01/02…），辅助排序。"""
        groups = await self.store.list_groups()
        unlabeled = [g for g in groups if not g.label]
        if not unlabeled:
            return 0
        taken = {g.label for g in groups if g.label}
        ordered = sorted(
            unlabeled,
            key=lambda g: (g.sort_order or 0, g.group_id),
        )
        use_digits = len(groups) > 26
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        seq = 0
        filled = 0
        for g in ordered:
            lab = ""
            for _ in range(999):
                nxt = str(seq + 1).zfill(2) if use_digits else letters[seq % 26]
                seq += 1
                if nxt not in taken:
                    lab = nxt
                    break
            if not lab:
                continue
            taken.add(lab)
            await self.store.update_group_fields(g.group_id, label=lab)
            filled += 1
        if filled:
            logger.info(f"[group-scan] auto-labeled {filled} groups")
        return filled

    async def rename_remote(
        self,
        group_id: str,
        name: str,
        display_name: str | None = None,
        label: str | None = None,
    ) -> None:
        """真实改名（set_group_name，群主权限；OpQueue 内执行）。

        校验后再回填：API 成功后回读群名，一致才写入本地 display_name/label
        （未校验不填充 —— 修复"修改未校验就填充到 Page"）。
        """
        from core.domain.enums import OneBotApiError, OneBotErrorKind

        await self.api.set_group_name(group_id, name)
        info = await self.api.get_group_info(group_id)
        actual = str((info or {}).get("group_name") or "")
        if actual != name:
            raise OneBotApiError(
                OneBotErrorKind.REMOTE_ERROR,
                "set_group_name",
                f"verify mismatch: got {actual!r} want {name!r}",
            )
        fields: dict = {}
        if display_name:
            fields["display_name"] = display_name
        if label is not None:
            fields["label"] = label
        if fields:
            await self.store.update_group_fields(group_id, **fields)

    async def list_page_groups(self, managed_groups: list[str]) -> list[GroupInfo]:
        """Page 可管理群清单。

        规则：白名单非空 → 白名单优先（显式列入的群恒可管理，含曾被移除的群）
              ∪ owned（我创建的群）；
              白名单为空 → 全部群可管理（managed=0 的已移除群除外）。
        """
        all_groups = await self.store.list_groups()
        mg = set(managed_groups or [])
        if mg:
            # 白名单优先：显式列入白名单的群恒可管理（含曾被移除 managed=0 的群）
            out = [
                g
                for g in all_groups
                if g.group_id in mg or (getattr(g, "managed", 1) and g.role == "owned")
            ]
            known = {g.group_id for g in all_groups}
            for gid in mg - known:
                out.append(GroupInfo(group_id=gid, role="unknown"))
            return out
        # 空白名单：放行所有（已移除 managed=0 的群除外）
        return [g for g in all_groups if getattr(g, "managed", 1)]

    async def is_page_managed(self, group_id: str, managed_groups: list[str]) -> bool:
        """Page 可管理判定（空白名单 → 放行所有群）。"""
        if not managed_groups:
            return True
        return any(
            g.group_id == group_id for g in await self.list_page_groups(managed_groups)
        )
