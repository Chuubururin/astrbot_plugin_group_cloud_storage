"""StorageGateway - storage gateway (cloud escaping / unified facade for
external services).

Routes three channel types; the business layer (Page/commands/external
clients) interacts only with this gateway:
1. cloud: adaptation/escaping layer toward the cloud (QQ/NapCat OneBot) -
   live session-handle resolution, capability probing, and rate-limited
   queueing, all consolidated in the OneBotApiPort adapter + OpQueue
2. local: local metadata index (SqliteMetaStore, filesystem-like/readable)
3. external: unified facade for external services (http/ftp/smb fetch
   ingress + the local download server)

This class is a lightweight facade (it does not duplicate implementations);
its job is to declare the routing semantics explicitly so other code can
read and integrate against them.
"""

from __future__ import annotations

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
        self.cloud = cloud  # cloud escaping: OneBotApiPort (NapCat adapter)
        self.local = local  # local index: MetaStorePort (SQLite)
        self.ingest = ingest  # cloud ingress (splitting/fetch)
        self.transfer = transfer  # egress transfer (http PUT/ftp/smb)
        self.dlserver = dlserver  # local download server (http/ftp)
        self.fileops = fileops  # file-level operations (upload/download/add/delete/modify)

    # ---------- Routing semantics ----------

    async def read(self, group_id: str, id: int) -> tuple[str, str]:
        """Read: unified download entry (local reassembly/direct link/streaming)."""
        if self.fileops is None:
            raise RuntimeError("gateway fileops not wired")
        return await self.fileops.download_info(group_id, id)

    async def write(
        self, group_id: str, staged_path: str, name: str, folder_id: str | None = None
    ) -> str:
        """Write: unified upload entry (volume splitting is automatic).

        Returns the queue task_id (str), like every other submit path.
        """
        if self.fileops is None:
            raise RuntimeError("gateway fileops not wired")
        return await self.fileops.submit_upload(group_id, staged_path, name, folder_id)

    async def ingest_url(
        self, group_id: str, url: str, name: str = "", to_album: bool = False
    ) -> str:
        """Ingress: fetch-import from an external URL (http/ftp/smb)."""
        if self.ingest is None:
            raise RuntimeError("gateway ingest not wired")
        return await self.ingest.submit_fetch(group_id, url, name, to_album)

    async def egress(self, group_id: str, id: int, target: str) -> str:
        """Egress: transfer to an external medium (http PUT/ftp/smb)."""
        if self.transfer is None:
            raise RuntimeError("gateway transfer not wired")
        return await self.transfer.submit_egress(group_id, id, target)

    async def probe_target(self, target: str) -> bool:
        """Probe: reachability test for an egress target."""
        if self.transfer is None:
            raise RuntimeError("gateway transfer not wired")
        return await self.transfer.probe_target(target)

    def serve_address(self, group_id: str, id: int) -> str:
        """Serve: local download service address (external clients pull
        directly)."""
        if self.dlserver is None or not getattr(self.dlserver, "enabled", False):
            raise RuntimeError("download server disabled")
        return self.dlserver.download_url(group_id, id)

    async def resolve_uri(self, uri: str) -> dict | None:
        """Locate: cloud:// URI -> local index row."""
        return await self.local.get_by_uri(uri)

    def describe(self) -> dict:
        """Capability description (programmatic self-description)."""
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
