"""StorageGateway —— v1.7 存储网关（对云端转义 / 对外部服务的统一门面）。

统一路由三类通道，业务层（Page/命令/外部客户端）只与本网关交互：
1. cloud：对云端（QQ/NapCat OneBot）的适配转义层 —— 会话句柄实时解析、
   能力探测、限速队列，全部收敛在 OneBotApiPort 适配器 + OpQueue
2. local：本地元数据索引（SqliteMetaStore，文件系统化/可读化）
3. external：对外部服务的统一门面（http/ftp/smb 拉取入库 + 本机下载服务）

本类为轻量门面（不复制实现），职责是**显式声明路由语义**，
程序可阅读并据此对接（docs/13）。
"""

from __future__ import annotations

from core.log import logger
from ports.meta_store import MetaStorePort
from ports.onebot_api import OneBotApiPort


class StorageGateway:
    def __init__(
        self,
        cloud: OneBotApiPort,
        local: MetaStorePort,
        ingest: object | None = None,
        transfer: object | None = None,
        dlserver: object | None = None,
        fileops: object | None = None,
    ):
        self.cloud = cloud  # 对云端转义：OneBotApiPort（NapCat 适配）
        self.local = local  # 本地索引：MetaStorePort（SQLite v10）
        self.ingest = ingest  # 云端入库（拆分/拉取）
        self.transfer = transfer  # 出库传输（http PUT/ftp/smb）
        self.dlserver = dlserver  # 本机下载服务（http/ftp）
        self.fileops = fileops  # 文件级操作（上传/下载/增删改）

    # ---------- 路由语义（docs/13 §3） ----------

    async def read(self, group_id: str, id: int) -> tuple[str, str]:
        """读：统一下载入口（本地重组/直链/流式）。"""
        if self.fileops is None:
            raise RuntimeError("gateway fileops not wired")
        return await self.fileops.download_info(group_id, id)

    async def write(
        self, group_id: str, staged_path: str, name: str, folder_id: str | None = None
    ) -> int:
        """写：统一上传入口（分卷自动）。"""
        if self.fileops is None:
            raise RuntimeError("gateway fileops not wired")
        return await self.fileops.submit_upload(group_id, staged_path, name, folder_id)

    async def ingest_url(
        self, group_id: str, url: str, name: str = "", to_album: bool = False
    ) -> str:
        """入：外部 URL（http/ftp/smb）拉取入库。"""
        if self.ingest is None:
            raise RuntimeError("gateway ingest not wired")
        return await self.ingest.submit_fetch(group_id, url, name, to_album)

    async def egress(self, group_id: str, id: int, target: str) -> str:
        """出：出库传输至外部介质（http PUT/ftp/smb）。"""
        if self.transfer is None:
            raise RuntimeError("gateway transfer not wired")
        return await self.transfer.submit_egress(group_id, id, target)

    async def probe_target(self, target: str) -> bool:
        """测：出库目标可达性测试。"""
        if self.transfer is None:
            raise RuntimeError("gateway transfer not wired")
        return await self.transfer.probe_target(target)

    def serve_address(self, group_id: str, id: int) -> str:
        """服务：本机下载服务地址（外部客户端直接拉取）。"""
        if self.dlserver is None or not getattr(self.dlserver, "enabled", False):
            raise RuntimeError("download server disabled")
        return self.dlserver.download_url(group_id, id)

    async def resolve_uri(self, uri: str) -> dict | None:
        """定位：cloud:// URI → 本地索引行。"""
        return await self.local.get_by_uri(uri)

    def describe(self) -> dict:
        """能力描述（程序化自述）。"""
        return {
            "cloud": "OneBot/NapCat（会话句柄转义 + 能力探测 + OpQueue 限速）",
            "local": f"SQLite schema v10（path/ext 文件系统化 + v_resources 视图）",
            "external": {
                "egress": ["http-put", "ftp", "smb"] if self.transfer else [],
                "http_download": bool(self.dlserver and self.dlserver.http_port),
                "ftp_download": bool(self.dlserver and self.dlserver.ftp_port),
                "ingress": ["http", "https", "ftp", "smb"] if self.ingest else [],
            },
        }
