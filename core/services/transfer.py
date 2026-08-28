"""TransferService —— 多协议传输管线（v2.7 模块化；v2.13 出库链路切除）。

入库（上云）：离线文件 —多协议传入(http/https/ftp/smb)→ 本机暂存 —OneBot11 上传→ QQ 服务器
（出库导出整链已随 16 清单 D3/F10/F11 裁定删除：出库由 OpenList 桥接承担，
  见 docs/00 §0.3 与 docs/05 v2.13。）

模块化：ProtocolAdapter 每协议实现 get（拉取到本地暂存），
协议细节（URL 解析/认证/限流/限大小）全部收敛于适配器。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit, unquote

import httpx

from core.services.op_queue import OpQueue
from ports.meta_store import MetaStorePort

FETCH_MAX_BYTES = 2 * 1024**3
FETCH_TIMEOUT_SEC = 180.0

_INGRESS_SCHEMES = ("http", "https", "ftp", "smb")


def parse_target(url: str) -> dict:
    """解析协议目标 URL → 结构化字段（认证/主机/路径）。"""
    p = urlsplit(url)
    return {
        "scheme": (p.scheme or "").lower(),
        "host": p.hostname or "",
        "port": p.port,
        "user": unquote(p.username) if p.username else "",
        "password": unquote(p.password) if p.password else "",
        "path": unquote(p.path) or "/",
        "url": url,
    }


class ProtocolAdapter:
    """单协议传输适配器：get（拉取到本地暂存）。"""

    scheme = ""

    def __init__(self, max_bytes: int, timeout: float):
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def get(self, target: dict, dest: Path) -> int:
        raise NotImplementedError


class HttpAdapter(ProtocolAdapter):
    scheme = "http"

    async def get(self, target: dict, dest: Path) -> int:
        total = 0
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout
        ) as client:
            async with client.stream("GET", target["url"]) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        fh.write(chunk)
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ValueError(
                                f"fetch exceeds max bytes ({self.max_bytes})"
                            )
        return total


class FtpAdapter(ProtocolAdapter):
    scheme = "ftp"

    def _conn(self, target: dict):
        import ftplib

        ftp = ftplib.FTP()
        ftp.connect(target["host"], target["port"] or 21, timeout=self.timeout)
        ftp.login(target["user"] or "anonymous", target["password"])
        return ftp

    async def get(self, target: dict, dest: Path) -> int:
        def _run():
            ftp = self._conn(target)
            try:
                written = 0

                def _cb(data: bytes) -> None:
                    nonlocal written
                    dest_fh.write(data)
                    written += len(data)
                    if written > self.max_bytes:
                        raise ValueError("fetch exceeds max bytes (ftp)")

                with dest.open("wb") as dest_fh:
                    ftp.retrbinary(f"RETR {target['path']}", _cb, blocksize=1 << 16)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()

        await asyncio.to_thread(_run)
        return dest.stat().st_size


class SmbAdapter(ProtocolAdapter):
    scheme = "smb"

    def _conn(self, target: dict):
        from smb.SMBConnection import SMBConnection

        share, _, _ = target["path"].lstrip("/").partition("/")
        if not share:
            raise ValueError("smb url needs share: smb://host/share/path")
        conn = SMBConnection(
            target["user"] or "guest",
            target["password"] or "",
            "astrbot",
            "astrbot",
            use_ntlm_v2=True,
        )
        if not conn.connect(target["host"], target["port"] or 445, timeout=int(self.timeout)):
            raise ValueError(f"smb connect failed: {target['host']}")
        return conn, share

    async def get(self, target: dict, dest: Path) -> int:
        def _run():
            conn, share = self._conn(target)
            try:
                _, _, path = target["path"].lstrip("/").partition("/")
                with dest.open("wb") as fh:
                    conn.retrieveFile(share, path, fh, timeout=int(self.timeout))
            finally:
                conn.close()

        await asyncio.to_thread(_run)
        return dest.stat().st_size


class TransferService:
    def __init__(
        self,
        store: MetaStorePort,
        queue: OpQueue,
        tmp_dir: Path,
        config: dict | None = None,
        download_info: Callable[..., Awaitable[tuple[str, str]]] | None = None,
    ):
        self.store = store
        self.queue = queue
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        cfg = config or {}
        fetch_max = int(cfg.get("fetch_max_bytes", FETCH_MAX_BYTES) or FETCH_MAX_BYTES)
        fetch_timeout = float(
            cfg.get("fetch_timeout_sec", FETCH_TIMEOUT_SEC) or FETCH_TIMEOUT_SEC
        )
        self._download_info = download_info
        self._adapters: dict[str, ProtocolAdapter] = {
            "http": HttpAdapter(fetch_max, fetch_timeout),
            "https": HttpAdapter(fetch_max, fetch_timeout),
            "ftp": FtpAdapter(fetch_max, fetch_timeout),
            "smb": SmbAdapter(fetch_max, fetch_timeout),
        }

    @staticmethod
    def parse_target(url: str) -> dict:
        return parse_target(url)

    def _adapter(self, t: dict, schemes: tuple[str, ...]) -> ProtocolAdapter:
        scheme = t["scheme"]
        if scheme not in schemes:
            raise ValueError(f"unsupported scheme: {scheme} (only {'/'.join(schemes)})")
        return self._adapters[scheme]

    async def download_to(self, url: str, dest: Path) -> int:
        """入库管线：多协议拉取到本地暂存；返回字节数。"""
        t = parse_target(url)
        return await self._adapter(t, _INGRESS_SCHEMES).get(t, dest)
