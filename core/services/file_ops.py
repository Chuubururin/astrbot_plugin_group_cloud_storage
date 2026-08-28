"""FileOpsService —— Page 文件级增删改查编排（P2/P3，docs/09 §13-14）。

- 上传（Create）：≤95MB 单文件；>95MB 分卷（WinRAR 模式：切割 → 逐卷 upload_group_file
  → 同步回填 file_id → volumes 映射；卷级断点续传）
- 下载（Read）：单文件直链代理 / 分卷拉取校验重组
- 更新（Update）：重命名/移动（OpQueue 内调 API + 库同步）
- 删除（Delete）：delete_group_file + 软删（级联清理分卷记录）
- 选群（StoragePlanner）：跨群存储 —— 上传目标群按余量自动选择（docs/09 §14.2）
全部外部调用经 OpQueue（限速/退避/SSE），DoD #1/#8。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path

from core.domain.enums import ResourceStatus, ResourceType
from core.domain.resource import Resource
from core.domain.sync import VolumeInfo
from core.log import logger
from core.services.op_queue import OpQueue
from core.services.resource_sync import ResourceSyncService
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort

# 分卷阈值/大小（docs/09 §14.1：≤95MB 单文件；分卷每卷默认 90MB 留余量）
CHUNK_THRESHOLD_BYTES = 95 * 1024 * 1024
VOLUME_SIZE_BYTES = 90 * 1024 * 1024


class FileOpsService:
    def __init__(
        self,
        api: OneBotApiPort,
        store: MetaStorePort,
        queue: OpQueue,
        sync: ResourceSyncService,
        tmp_dir: Path,
        page_size: int = 100,
        planner=None,
    ):
        from core.services.storage_planner import StoragePlanner

        self.api = api
        self.store = store
        self.queue = queue
        self.sync = sync
        self._planner = planner or StoragePlanner(store)
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.page_size = page_size
        self._sync_locks: dict[str, asyncio.Lock] = {}
        # 列表缓存：同批分片解析复用一次云端列表（5s 内有效），
        # N 分片下载从 N 次列表调用降为 1 次
        self._list_cache: dict[tuple, tuple[float, object]] = {}

    # ---------- 上传（Page 暂存 → 群） ----------

    async def submit_upload(
        self, group_id: str, staged_path: str, name: str, folder_id: str | None = None
    ) -> str:
        return await self.queue.submit(
            "upload",
            target=group_id,
            payload={"path": staged_path, "name": name, "folder_id": folder_id or ""},
        )

    async def _do_upload(self, op) -> None:
        path = op.payload["path"]
        name = op.payload["name"]
        folder = op.payload.get("folder_id") or None
        logger.info(f"[file-ops] upload {name} -> group {op.target} ({path})")
        src = Path(path)
        if not src.exists() or not src.is_file():
            raise ValueError(f"staged file missing: {path}")
        size = src.stat().st_size
        # 容量溢出切换：仅单文件直传（分卷保持原目标，避免 parent 映射不一致）
        if not op.payload.get("parent_resource_id") and size > 0:
            groups = [
                g for g in await self.store.list_groups() if getattr(g, "managed", 1)
            ]
            pick = await self._planner.pick_group(groups, requested_bytes=size)
            if pick is not None and str(pick.group_id) != str(op.target):
                logger.info(
                    f"[file-ops] upload target switch {op.target} -> {pick.group_id} "
                    f"(capacity-aware)"
                )
                op.target = pick.group_id
        if size > CHUNK_THRESHOLD_BYTES and op.payload.get("parent_resource_id"):
            # 分卷（WinRAR 模式）：大文件逐卷上传 + 校验重组（云端永久化）
            await self._do_volume_upload(
                op, src, name, op.payload["parent_resource_id"], folder
            )
        else:
            await self.api.upload_group_file(op.target, path, name, folder_id=folder)
        # 上传后触发全量同步，把新文件纳入索引（单文件 source_ref 由列表接口返回）
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if not result.ok:
            logger.warning(f"[file-ops] post-upload sync failed: {result.error}")
        # 暂存清理
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- 删除 ----------

    async def submit_delete(self, group_id: str, id: int) -> str:
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        task_id = await self.queue.submit(
            "delete",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
            },
        )
        # v15：操作流记录（删除类云端不可逆 → 撤销明示"不可撤销"）
        await self.queue.record_op(
            task_id,
            "delete",
            before={"group_id": group_id, "id": id, "name": detail["name"]},
            after={},
        )
        return task_id

    async def _do_delete(self, op) -> None:
        detail = await self.store.get_resource_detail(op.target, op.payload["id"])
        if detail and (detail.get("meta") or {}).get("volumes"):
            # 分卷资源：逐卷按各自群删除（跨群存储）→ 级联清理卷记录 → 父软删
            vols = await self.store.list_volumes(detail["resource_id"])
            for v in vols:
                if not v.source_ref:
                    continue
                vg = v.group_id or op.target
                fresh = await self._resolve_file_ref(
                    vg,
                    v.part_name,
                    int(v.size or 0),
                    detail.get("folder_id") or None,
                )
                if fresh is None:
                    # 云端已不存在（新 NapCat 会话句柄：旧 id 不可回退）→ 跳过
                    logger.info(f"[file-ops] part {v.part_name} already gone")
                    continue
                await self.api.delete_group_file(vg, fresh[0], fresh[1])
            await self.store.remove_volumes(detail["resource_id"])
            await self.store.update_resource_fields(
                op.payload["id"], status=ResourceStatus.DELETED.value
            )
            logger.info(
                f"[file-ops] deleted volume-resource {detail['resource_id']} "
                f"({len(vols)} parts across groups)"
            )
            return
        fresh = await self._resolve_file_ref(
            op.target,
            (detail or {}).get("name") or op.payload.get("name") or "",
            0,
            (detail or {}).get("folder_id") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"])
        try:
            await self.api.delete_group_file(op.target, fid, busid)
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in ("invalid", "not found", "不存在")):
                logger.info(f"[file-ops] delete target already gone: {e}")
            else:
                raise
        await self.store.update_resource_fields(
            op.payload["id"], status=ResourceStatus.DELETED.value
        )
        logger.info(f"[file-ops] deleted {op.payload['file_id']} in {op.target}")

    # ---------- 重命名 / 移动 ----------

    async def submit_replace_name(self, group_id: str, id: int, new_name: str) -> str:
        """改名（下载-重传语义）：下载原件 → 新名重传 → 删除旧文件。"""
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        # 分卷资源不支持本操作（体积过大）
        if (detail.get("meta") or {}).get("volumes"):
            raise ValueError("分卷资源不支持改名重传")
        task_id = await self.queue.submit(
            "replace_name",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "name": detail["name"],
                "new_name": new_name,
                "folder": detail.get("folder_id") or "",
            },
        )
        # v15：操作流记录（撤销=原状恢复）
        await self.queue.record_op(
            task_id,
            "replace_name",
            before={"group_id": group_id, "id": id, "name": detail["name"]},
            after={"group_id": group_id, "id": id, "name": new_name},
        )
        return task_id

    async def _do_replace_name(self, op) -> None:
        """下载原件字节 → 新名重传（同群）→ 删除旧文件 → 索引替换。"""
        import time as _t

        fresh = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"] or 0)
        data = await self._fetch_bytes(
            await self.api.get_group_file_url(
                op.target,
                fid,
                busid,
                op.payload["name"],
            )
        )
        if not data:
            raise ValueError("download returned empty content")
        staged = self.tmp_dir / f"replace_{op.payload['id']}_{int(_t.time())}.tmp"
        staged.write_bytes(data)
        await self.api.upload_group_file(
            op.target,
            staged.as_posix(),
            op.payload["new_name"],
            folder_id=op.payload.get("folder") or None,
        )
        # 删除旧文件（重新解析新鲜 id：旧名在重传后仍存在）
        fresh2 = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid2, busid2 = fresh2 or (op.payload["file_id"], op.payload["busid"] or 0)
        await self.api.delete_group_file(op.target, fid2, busid2)
        staged.unlink(missing_ok=True)
        # 索引替换：新文件名（新 source_ref 由后续文件刷新回填；旧记录软删）
        await self.store.update_resource_fields(
            op.payload["id"], name=op.payload["new_name"]
        )
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        await self.sync.run_full_sync(op.target, lock)
        logger.info(
            f"[file-ops] replaced {op.payload['name']} -> "
            f"{op.payload['new_name']} in {op.target}"
        )

    async def submit_move(self, group_id: str, id: int, folder_id: str) -> str:
        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        task_id = await self.queue.submit(
            "move_file",
            target=group_id,
            payload={
                "id": id,
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "folder_id": folder_id,
                "folder": detail.get("folder_id") or "",
            },
        )
        # v15：操作流记录（撤销=反向移动）
        await self.queue.record_op(
            task_id,
            "move",
            before={
                "group_id": group_id,
                "id": id,
                "folder": detail.get("folder_id") or "",
            },
            after={"group_id": group_id, "id": id, "folder": folder_id},
        )
        return task_id

    async def _do_move(self, op) -> None:
        detail = await self.store.get_resource_detail(op.target, op.payload["id"])
        cpd = (
            (op.payload.get("folder") or "!/")
            if (op.payload.get("folder") or "").strip()
            else "!/"
        )
        fresh = await self._resolve_file_ref(
            op.target,
            (detail or {}).get("name") or "",
            0,
            (detail or {}).get("folder_id") or None,
        )
        fid = fresh[0] if fresh else op.payload["file_id"]
        await self.api.move_group_file(
            op.target,
            fid,
            cpd,
            str(op.payload.get("folder_id") or "!/"),
        )
        await self.store.update_resource_fields(
            op.payload["id"], folder_id=op.payload["folder_id"]
        )

    # ---------- 下载 ----------

    # ---------- 分卷（WinRAR 模式，docs/09 §14.1） ----------

    async def _do_volume_upload(
        self, op, src: Path, name: str, parent_id: str, folder: str | None
    ) -> None:
        """切割 → 逐卷上传（跳过已上传卷=断点续传）。"""
        import hashlib

        total_sha = hashlib.sha256()
        # volumes 主键与 resources 一致：完整 resource_id（下载/回填按 detail 查询）
        parent_key = op.payload.get("parent_resource_id_full") or parent_id
        cut_dir = self.tmp_dir / f"vol_{parent_id}"
        cut_dir.mkdir(parents=True, exist_ok=True)
        volumes: list[VolumeInfo] = []
        with src.open("rb") as fh:
            seq = 1
            while True:
                chunk = fh.read(VOLUME_SIZE_BYTES)
                if not chunk:
                    break
                total_sha.update(chunk)
                part_name = f"{Path(name).stem}.part{seq:02d}"
                (cut_dir / part_name).write_bytes(chunk)
                volumes.append(
                    VolumeInfo(
                        parent_resource_id=parent_key,
                        seq=seq,
                        part_name=part_name,
                        size=len(chunk),
                        sha256=hashlib.sha256(chunk).hexdigest(),
                        status="pending",
                    )
                )
                seq += 1
        await self.store.insert_volumes(volumes)  # 幂等注册（保留已存在卷状态）
        total_count = len(volumes)
        for v in volumes:
            cur = next(
                (
                    x
                    for x in await self.store.list_volumes(parent_key)
                    if x.seq == v.seq
                ),
                v,
            )
            if cur.status == "uploaded" and cur.source_ref:
                continue  # 已上传 → 断点续传跳过
            await self.store.update_volume_fields(parent_key, v.seq, status="uploading")
            await self.api.upload_group_file(
                op.target,
                (cut_dir / v.part_name).as_posix(),
                v.part_name,
                folder_id=folder,
            )
            await self.store.update_volume_fields(
                parent_key,
                v.seq,
                status="uploaded",
                sha256=v.sha256,
                size=v.size,
            )
            self.queue.publish(
                {
                    "type": "progress",
                    "kind": "vol_upload",
                    "target": op.target,
                    "i": v.seq,
                    "n": total_count,
                    "part": v.part_name,
                }
            )
        # 父资源 meta 记录总 sha256（下载重组校验依据）
        import json

        detail = await self.store.get_resource_by_resource_id(
            op.payload.get("parent_resource_id_full") or parent_id
        )
        if detail:
            from core.composition.spec import encode_composition

            meta = dict(detail["meta"] or {})
            meta["volumes"] = True
            if op.payload.get("compress"):
                meta["compression"] = "zip"
                meta["original_name"] = op.payload.get("name") or name
            meta["total_sha256"] = total_sha.hexdigest()
            meta["composition"] = encode_composition(
                "volumes", total_count, "binary", total_sha.hexdigest()
            )
            await self.store.update_resource_fields(
                detail["id"], meta=json.dumps(meta, ensure_ascii=False)
            )
        logger.info(f"[file-ops] volume upload done: {name} -> {len(volumes)} parts")
        # 清理本地分卷切片
        for v in volumes:
            (cut_dir / v.part_name).unlink(missing_ok=True)
        try:
            cut_dir.rmdir()
        except OSError:
            pass

    async def pick_upload_group(self) -> str | None:
        """自动选群（群排序顺序：owned 优先 + sort_order）；无容量数据时按排序取。"""
        groups = await self.store.list_groups()
        candidates = [g for g in groups if getattr(g, "managed", 1)]
        pick = await self._planner.pick_group(candidates)
        return pick.group_id if pick else None

    async def recommend_upload_group(
        self, kind: str = "file", requested_bytes: int = 0
    ) -> dict | None:
        """2026-09-01 N-07：推荐上传群（缺省规则端点数据源）。

        - kind=file（群文件）：群号值最小同时剩余空间 > 待上传大小的群；
        - kind=album/essence（相册/精华）：群号值最小的群（上限未知，不做容量预检）；
        - 候选仅含受管且在线（hidden=0）的群；无候选返回 None。
        """
        groups = await self.store.list_groups()
        candidates = [g for g in groups if getattr(g, "managed", 1)]
        if not candidates:
            return None
        if kind in ("album", "essence"):
            pick = await self._planner.pick_min_group_id(candidates)
        else:
            pick = await self._planner.pick_min_group_for_size(
                candidates, requested_bytes=int(requested_bytes or 0)
            )
        if pick is None:
            return None
        return {
            "group_id": pick.group_id,
            "group_name": pick.shown_name or pick.group_name or pick.group_id,
            "role": pick.role,
            "sort_order": pick.sort_order,
        }

    async def submit_volume_upload(
        self,
        group_id: str,
        staged_path: str,
        name: str,
        folder_id: str | None = None,
        compress: bool = False,
    ) -> str:
        """分卷上传入口：预建父资源（meta.volumes 标记）→ 入队。

        2026-09-03 C-4（可选分卷压缩，可逆）：compress=True 时先 zip 打包
        （标准库 zipfile，arcname=源文件名）再切卷；meta 记 compression=zip、
        original_size/original_name——下载重组经 sha256 校验 zip 后解压还原。
        """
        import uuid as _uuid

        parent_id = f"volgroup:{_uuid.uuid4().hex[:10]}"
        meta: dict = {"volumes": True}
        if compress:
            import zipfile

            src = Path(staged_path)
            zip_path = self.tmp_dir / f"zip_{_uuid.uuid4().hex[:10]}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(src, arcname=name)
            staged_path = zip_path.as_posix()
            meta.update(
                compression="zip",
                original_size=src.stat().st_size,
                original_name=name,
            )
        total = Path(staged_path).stat().st_size
        await self.store.upsert_resources(
            [
                Resource(
                    group_id=group_id,
                    type=ResourceType.FILE,
                    name=name,
                    source_ref=parent_id,
                    size=total,
                    created_at=int(time.time()),
                    meta=meta,
                )
            ]
        )
        return await self.queue.submit(
            "upload",
            target=group_id,
            payload={
                "path": staged_path,
                "name": name,
                "folder_id": folder_id or "",
                "parent_resource_id": parent_id,
                "parent_resource_id_full": f"{group_id}:file:{parent_id}",
            },
        )

    async def backfill_volume_refs(
        self, group_id: str, parent_resource_id_full: str
    ) -> None:
        """同步后回填分卷的 source_ref/busid（upload_group_file 不返回 file_id）。"""
        from core.domain.sync import ResourceQuery as _RQ

        vols = await self.store.list_volumes(parent_resource_id_full)
        if not vols:
            return
        page = await self.store.query_resources(_RQ(group_id=group_id, page_size=200))
        by_name = {it.name: it for it in page.items}
        for v in vols:
            hit = by_name.get(v.part_name)
            if hit:
                await self.store.update_volume_fields(
                    parent_resource_id_full,
                    v.seq,
                    source_ref=hit.source_ref,
                    busid=hit.busid or 0,
                )
        logger.info(f"[file-ops] volume backfill: {len(vols)} parts -> {group_id}")

    def _is_volume_resource(self, detail: dict) -> bool:
        return bool((detail.get("meta") or {}).get("volumes"))

    async def _resolve_file_ref(
        self,
        group_id: str,
        name: str,
        size: int = 0,
        folder_id: str | None = None,
    ) -> tuple[str, int] | None:
        """按名称实时解析新鲜 file_id。

        NapCat 的 file_id 是会话内随机句柄（跨重启/超时失效），
        库中存量 id 仅作回退；操作前用实时列表按名+大小解析。
        """
        key = (str(group_id), str(folder_id or ""))
        now = time.monotonic()
        hit = self._list_cache.get(key)
        if hit and now - hit[0] < 5.0:
            lst = hit[1]
        else:
            try:
                lst = (
                    await self.api.list_group_folder(group_id, folder_id)
                    if folder_id
                    else await self.api.list_group_root(group_id)
                )
                self._list_cache[key] = (now, lst)
            except Exception as e:
                logger.debug(f"[file-ops] fresh resolve list failed for {name}: {e}")
                return None
        cands = [f for f in lst.files if f.name == name]
        if not cands:
            return None
        if size > 0:
            exact = [f for f in cands if f.size == size]
            if exact:
                cands = exact
        f = cands[0]
        return str(f.file_id), int(f.busid or 0)

    async def download_info(self, group_id: str, id: int) -> tuple[str, str]:
        """返回 (下载目标, 文件名)。

        - 单文件：返回实时直链（调用方流式代理）
        - 分卷：拉全卷 → 逐卷 sha256 校验 → 按序合并 → 返回本地重组临时文件
        """
        import hashlib

        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        name = detail["name"]
        if not self._is_volume_resource(detail):
            fresh = await self._resolve_file_ref(
                group_id,
                name,
                int(detail.get("size") or 0),
                detail.get("folder_id") or None,
            )
            fid, busid = fresh or (detail["source_ref"], detail["busid"] or 0)
            url = await self.api.get_group_file_url(group_id, fid, busid, name)
            return url, name
        vols = await self.store.list_volumes(detail["resource_id"])
        if not vols or any(not v.source_ref for v in vols):
            raise ValueError("volume refs not ready (仍在上传/回填中)")
        kind = (detail.get("meta") or {}).get("kind") or "bytes"
        if kind == "video":
            return await self._recon_video(
                group_id, name, vols, detail.get("folder_id") or None
            )
        out = self.tmp_dir / f"recon_{uuid.uuid4().hex[:10]}_{name}"
        with out.open("wb") as of:
            for v in vols:
                # 跨群分卷：每卷按 group_id 取 URL（兼容旧数据回退父群）
                vg = v.group_id or group_id
                fresh = await self._resolve_file_ref(
                    vg,
                    v.part_name,
                    int(v.size or 0),
                    detail.get("folder_id") or None,
                )
                fid, busid = fresh or (v.source_ref, v.busid or 0)
                url = await self.api.get_group_file_url(vg, fid, busid, v.part_name)
                data = await self._fetch_bytes(url)
                if v.sha256 and hashlib.sha256(data).hexdigest() != v.sha256:
                    out.unlink(missing_ok=True)
                    raise ValueError(f"volume {v.seq} sha256 mismatch")
                of.write(data)
        meta_total = (detail.get("meta") or {}).get("total_sha256")
        if meta_total:
            if hashlib.sha256(out.read_bytes()).hexdigest() != meta_total:
                out.unlink(missing_ok=True)
                raise ValueError("total sha256 mismatch")
        # 2026-09-03 C-4：zip 压缩分卷 → 校验后解压还原（可逆）
        compression = (detail.get("meta") or {}).get("compression")
        if compression == "zip":
            import zipfile

            extract_dir = self.tmp_dir / f"unzip_{uuid.uuid4().hex[:10]}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(out) as zf:
                zf.extractall(extract_dir)
            inner_name = (detail.get("meta") or {}).get("original_name") or name
            inner = extract_dir / inner_name
            if not inner.exists():
                raise ValueError(f"zip reassemble missing {inner_name}")
            out.unlink(missing_ok=True)
            return inner.as_posix(), inner_name
        return out.as_posix(), name

    async def _recon_video(
        self, group_id: str, name: str, vols, folder: str | None = None
    ) -> tuple[str, str]:
        """长视频分片重组：逐段拉取（sha256 校验）→ ffmpeg concat 合并。返回 (path, name)。"""
        import subprocess
        import shutil as _sh

        if not _sh.which("ffmpeg"):
            raise ValueError("ffmpeg not available for video reassemble")
        seg_dir = self.tmp_dir / f"vidrecon_{uuid.uuid4().hex[:10]}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        out = self.tmp_dir / f"recon_{uuid.uuid4().hex[:10]}_{name}"
        try:
            list_file = seg_dir / "concat.txt"
            with list_file.open("w", encoding="utf-8") as lf:
                for v in vols:
                    vg = v.group_id or group_id
                    fresh = await self._resolve_file_ref(
                        vg, v.part_name, int(v.size or 0), folder
                    )
                    fid, busid = fresh or (v.source_ref, v.busid or 0)
                    url = await self.api.get_group_file_url(vg, fid, busid, v.part_name)
                    data = await self._fetch_bytes(url)
                    if v.sha256 and hashlib.sha256(data).hexdigest() != v.sha256:
                        raise ValueError(f"volume {v.seq} sha256 mismatch")
                    seg = seg_dir / v.part_name
                    seg.write_bytes(data)
                    lf.write(f"file '{seg.as_posix()}'\n")

            def _concat():
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        list_file.as_posix(),
                        "-c",
                        "copy",
                        out.as_posix(),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=14400,
                )
                if proc.returncode != 0:
                    raise ValueError(f"ffmpeg concat failed: {proc.stderr[-300:]}")

            import asyncio as _aio

            await _aio.to_thread(_concat)
            return out.as_posix(), name
        finally:
            for f in seg_dir.glob("*"):
                if f.is_file() and f != out:
                    f.unlink(missing_ok=True)
            try:
                seg_dir.rmdir()
            except OSError:
                pass

    async def verify_volumes(
        self, group_id: str, parent_resource_id_full: str, total_sha256: str | None
    ) -> dict:
        """分卷完整性校验：逐卷拉取 → sha256 比对 → 汇总 hash 比对（不落盘重组）。"""
        import hashlib

        vols = await self.store.list_volumes(parent_resource_id_full)
        if not vols or any(not v.source_ref for v in vols):
            return {
                "mode": "volumes",
                "ok": False,
                "message": "分卷引用未就绪（上传/回填中）",
            }
        parts = []
        digest = hashlib.sha256()
        checks = 0
        for v in vols:
            vg = v.group_id or group_id
            fresh = await self._resolve_file_ref(vg, v.part_name, int(v.size or 0))
            fid, busid = fresh or (v.source_ref, v.busid or 0)
            url = await self.api.get_group_file_url(vg, fid, busid, v.part_name)
            data = await self._fetch_bytes(url)
            actual = hashlib.sha256(data).hexdigest()
            ok = (not v.sha256) or actual == v.sha256
            checks += int(ok)
            digest.update(data)
            parts.append(
                {"seq": v.seq, "part": v.part_name, "size": len(data), "sha_ok": ok}
            )
        total_ok = (not total_sha256) or digest.hexdigest() == total_sha256
        ok = checks == len(vols) and total_ok
        return {
            "mode": "volumes",
            "ok": ok,
            "parts": parts,
            "parts_ok": f"{checks}/{len(vols)}",
            "total_sha256_ok": total_ok,
            "message": "分卷校验通过" if ok else "分卷校验失败（请重试下载或重新上传）",
        }

    async def _fetch_bytes(self, url: str) -> bytes:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def submit_create_folder(self, group_id: str, name: str) -> str:
        """新建群文件目录（v1.6：对接 create_group_file_folder）。"""
        if not (0 < len(name) <= 60):
            raise ValueError("folder name length 1..60")
        return await self.queue.submit(
            "create_folder",
            target=group_id,
            payload={"name": name},
        )

    async def _do_create_folder(self, op) -> None:
        await self.api.create_group_file_folder(op.target, op.payload["name"])
        # 目录实体刷新（列表/下拉即时可见）
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        result = await self.sync.run_full_sync(op.target, lock)
        if not result.ok:
            logger.warning(f"[file-ops] post-folder sync failed: {result.error}")
        logger.info(f"[file-ops] folder created: {op.payload['name']} in {op.target}")

    # ---------- op 分发（由 Main._op_handler 调用） ----------

    async def submit_convert_volumes(self, group_id: str, id: int, compress: bool = False) -> str:
        """化整为零（v2.8）：云端已有大文件 → 下载 → 分卷 → 逐卷上传 → 删原件。

        2026-09-03 C-4：compress=True 时先 zip 打包源文件再切卷（可逆；重组后解压）。
        """
        from core.composition.spec import is_composite

        detail = await self.store.get_resource_detail(group_id, id)
        if not detail:
            raise ValueError(f"resource {id} not found in group {group_id}")
        if is_composite(detail.get("meta")):
            raise ValueError("该资源已是组合形态（分卷/分片），无需转换")
        if int(detail.get("size") or 0) <= CHUNK_THRESHOLD_BYTES:
            raise ValueError(
                f"文件小于分卷阈值（{CHUNK_THRESHOLD_BYTES // 1048576}MB）"
            )
        return await self.queue.submit(
            "convert_volumes",
            target=group_id,
            payload={
                "id": id,
                "name": detail["name"],
                "folder": detail.get("folder_id") or "",
                "file_id": detail["source_ref"],
                "busid": detail["busid"] or 0,
                "resource_id": detail["resource_id"],
                "compress": bool(compress),
            },
        )

    async def _do_convert_volumes(self, op) -> None:
        """下载原件 → 复用分卷管线上传 → 删除云端原件 → 索引同步。"""
        fresh = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid, busid = fresh or (op.payload["file_id"], op.payload["busid"] or 0)
        data = await self._fetch_bytes(
            await self.api.get_group_file_url(op.target, fid, busid, op.payload["name"])
        )
        if not data:
            raise ValueError("download returned empty content")
        src = self.tmp_dir / f"conv_{op.payload['id']}_{uuid.uuid4().hex[:8]}.tmp"
        src.write_bytes(data)
        # 2026-09-03 C-4：可选 zip 压缩后再切卷（下载重组自动解压还原）
        if op.payload.get("compress"):
            import zipfile

            zip_path = self.tmp_dir / f"convzip_{op.payload['id']}_{uuid.uuid4().hex[:8]}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(src, arcname=op.payload["name"])
            src.unlink(missing_ok=True)
            src = zip_path
        try:
            # 复用分卷上传管线：以现有 resource_id 为父键（索引原位转换）
            op.payload["parent_resource_id"] = op.payload["id"]
            op.payload["parent_resource_id_full"] = op.payload["resource_id"]
            await self._do_volume_upload(
                op,
                src,
                op.payload["name"],
                op.payload["id"],
                op.payload.get("folder") or None,
            )
        finally:
            src.unlink(missing_ok=True)
        # 删除云端原件（重传后旧文件仍在）
        fresh2 = await self._resolve_file_ref(
            op.target,
            op.payload["name"],
            0,
            op.payload.get("folder") or None,
        )
        fid2, busid2 = fresh2 or (op.payload["file_id"], op.payload["busid"] or 0)
        await self.api.delete_group_file(op.target, fid2, busid2)
        lock = self._sync_locks.setdefault(op.target, asyncio.Lock())
        await self.sync.run_full_sync(op.target, lock)
        logger.info(f"[file-ops] converted {op.payload['name']} to volumes")

    async def handle(self, op) -> None:
        if op.kind == "upload":
            await self._do_upload(op)
        elif op.kind == "delete":
            await self._do_delete(op)
        elif op.kind == "move_file":
            await self._do_move(op)
        elif op.kind == "replace_name":
            await self._do_replace_name(op)
        elif op.kind == "create_folder":
            await self._do_create_folder(op)
        elif op.kind == "convert_volumes":
            await self._do_convert_volumes(op)
        else:
            raise ValueError(f"unknown file op kind: {op.kind}")
