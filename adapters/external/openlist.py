"""OpenListClient -- httpx control plane thin client (~250 lines).

Implements REQ-05 (capability probe & degradation), REQ-06 (URL not persisted),
REQ-07 (idempotency), REQ-09 (DTO up), REQ-13 (auto-pagination).

Dependencies:
- httpx (host dependency, explicitly declared in requirements.txt)
- adapters/external/base.py (error types, SSRF protection, state normalization)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from core.log import logger

from .base import (
    ErrorKind,
    ExternalApiError,
    OpenListApiError,
    classify_error,
    normalize_task_state,
    validate_base_url,
)


# DTOs (REQ-09: dataclass, no bare dict across layers)


@dataclass(frozen=True)
class OfflineTask:
    """Offline download task representation."""

    id: str
    name: str
    state: str
    status: str
    progress: float
    error: str


@dataclass(frozen=True)
class NetFile:
    """File/directory entry from remote listing."""

    name: str
    size: int
    is_dir: bool
    modified: str
    sign: str = ""


@dataclass(frozen=True)
class DirectLink:
    """Direct URL for file access (memory-only, REQ-06: not persisted)."""

    url: str


# Task state enum for clarity
# 统一使用 core.domain.enums.BridgeTaskState.from_external() 映射
_TASK_STATE_MAP = None  # 已迁移至 BridgeTaskState.from_external()


def _normalize_task_state(state) -> str:
    """Normalize task state from OpenList to internal representation.

    Handles both string and integer state values from OpenList API.
    Delegates to BridgeTaskState.from_external() for single source of truth.
    """
    from core.domain.enums import BridgeTaskState

    return BridgeTaskState.from_external(state).value


class OpenListClient:
    """Thin async client for OpenList REST API.

    Control-plane only: manages tasks, metadata, and file operations.
    No file content transfer (data-plane stays with dlserver/OpenList downloader).

    Features:
    - Lazy httpx.AsyncClient initialization
    - Automatic 401/403 re-login with single replay (REQ-05)
    - Envelope error handling
    - SSRF protection (REQ-11)
    - Token/password log sanitization
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        timeout: float = 30.0,
        allow_private_address: bool = False,
    ):
        # Validate base URL (REQ-11 SSRF protection)
        self._base_url = validate_base_url(
            base_url, allow_private=allow_private_address
        )
        self._username = username
        self._password = password
        self._token = token
        self._timeout = timeout
        self._allow_private = allow_private_address

        # Lazy-initialized httpx client
        self._client: httpx.AsyncClient | None = None

        # Capability state (REQ-05)
        self._capability: str = "UNKNOWN"  # UNKNOWN | OK | BROKEN
        self._ping_failures: int = 0

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy-create httpx.AsyncClient."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close httpx client and optionally logout."""
        if self._client is not None:
            # Best-effort logout (don't fail on error)
            try:
                await self._request_no_retry("GET", "/api/auth/logout")
            except Exception:
                pass
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        """Health check (REQ-05).

        Returns True if OpenList is reachable and healthy.
        On failure, sets capability to BROKEN and increases backoff.
        """
        try:
            client = await self._ensure_client()
            resp = await client.get("/ping", timeout=5.0)
            # OpenList /ping returns 200 with empty body or JSON
            if resp.status_code == 200:
                self._capability = "OK"
                self._ping_failures = 0
                return True
            self._capability = "BROKEN"
            self._ping_failures += 1
            return False
        except Exception as e:
            logger.warning(f"[openlist] ping failed: {e}")
            self._capability = "BROKEN"
            self._ping_failures += 1
            return False

    async def ensure_token(self) -> str:
        """Ensure valid authentication token.

        Priority:
        1. Injected token (from config)
        2. Login with username/password
        """
        if self._token:
            return self._token

        if not self._username or not self._password:
            raise OpenListApiError(
                "No token or credentials configured for OpenList authentication"
            )

        # Login
        client = await self._ensure_client()
        resp = await client.post(
            "/api/auth/login",
            json={
                "username": self._username,
                "password": self._password,
            },
        )

        if resp.status_code != 200:
            raise OpenListApiError(
                f"Login failed: HTTP {resp.status_code}",
                code=resp.status_code,
            )

        data = resp.json()
        if data.get("code") != 200:
            raise OpenListApiError(
                f"Login failed: {data.get('message', 'unknown error')}",
                code=data.get("code"),
            )

        token = data.get("data", {}).get("token")
        if not token:
            raise OpenListApiError("Login response missing token")

        self._token = token
        logger.info("[openlist] authentication successful")
        return token

    async def _request_no_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Single request attempt without retry logic. Returns raw httpx.Response."""
        client = await self._ensure_client()
        headers = {}

        # Inject auth token
        try:
            token = await self.ensure_token()
            headers["Authorization"] = token
        except OpenListApiError:
            # Allow unauthenticated requests for /ping and /api/auth/login
            if path not in ("/ping", "/api/auth/login", "/api/auth/logout"):
                raise

        resp = await client.request(
            method,
            path,
            json=json,
            params=params,
            headers=headers,
        )
        return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request with automatic 401/403 re-login (REQ-05).

        Behavior:
        1. Attach Authorization header
        2. On 401/403: re-login and replay once (only once)
        3. On envelope code != 200: raise OpenListApiError
        4. Network errors: classify and raise ExternalApiError
        """
        resp = await self._request_no_retry(method, path, json=json, params=params)

        # Handle 401/403: re-login and retry once
        if resp.status_code in (401, 403):
            logger.info("[openlist] auth expired, re-logging in")
            self._token = ""  # Clear cached token
            try:
                resp = await self._request_no_retry(
                    method, path, json=json, params=params
                )
            except Exception as e:
                raise ExternalApiError(
                    "openlist",
                    f"Re-login and retry failed: {e}",
                    code=resp.status_code,
                )

        # Check HTTP status
        if resp.status_code >= 400:
            raise OpenListApiError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                code=resp.status_code,
            )

        # Parse envelope
        try:
            data = resp.json()
        except Exception as e:
            raise OpenListApiError(f"Invalid JSON response: {e}")

        # Check envelope code
        code = data.get("code")
        if code is not None and code != 200:
            message = data.get("message", "unknown error")
            raise OpenListApiError(message, code=code)

        return data

    async def submit_offline_download(
        self,
        urls: list[str],
        path: str,
        *,
        tool: str = "SimpleHttp",
        delete_policy: str = "delete_on_upload_succeed",
    ) -> list[OfflineTask]:
        """Submit offline download task (REQ-01: only generate/submit links, no file IO).

        Args:
            urls: List of direct download URLs
            path: Target directory path on OpenList
            tool: Download tool (SimpleHttp, aria2, qBittorrent)
            delete_policy: When to delete source

        Returns:
            List of submitted OfflineTask objects
        """
        data = await self._request(
            "POST",
            "/api/fs/add_offline_download",
            json={
                "urls": urls,
                "path": path,
                "tool": tool,
                "delete_policy": delete_policy,
            },
        )

        tasks_data = data.get("data", {}).get("tasks", [])
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=_normalize_task_state(t.get("state", "")),
                status=t.get("status", ""),
                progress=float(t.get("progress", 0)),
                error=t.get("error", ""),
            )
            for t in tasks_data
        ]

    async def tasks_undone(self) -> list[OfflineTask]:
        """Get list of undone offline download tasks."""
        data = await self._request("GET", "/api/task/offline_download/undone")
        tasks_data = data.get("data", [])
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=_normalize_task_state(t.get("state", "")),
                status=t.get("status", ""),
                progress=float(t.get("progress", 0)),
                error=t.get("error", ""),
            )
            for t in tasks_data
        ]

    async def tasks_done(self) -> list[OfflineTask]:
        """Get list of completed offline download tasks."""
        data = await self._request("GET", "/api/task/offline_download/done")
        tasks_data = data.get("data", [])
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=_normalize_task_state(t.get("state", "")),
                status=t.get("status", ""),
                progress=float(t.get("progress", 0)),
                error=t.get("error", ""),
            )
            for t in tasks_data
        ]

    async def task_cancel(self, tid: str) -> bool:
        """Cancel an offline download task."""
        data = await self._request(
            "POST",
            "/api/task/offline_download/cancel",
            params={"tid": tid},
        )
        return data.get("code") == 200

    async def task_retry(self, tid: str) -> bool:
        """Retry a failed offline download task."""
        data = await self._request(
            "POST",
            "/api/task/offline_download/retry",
            params={"tid": tid},
        )
        return data.get("code") == 200

    async def task_clear_done(self) -> bool:
        """Clear all completed offline download tasks."""
        data = await self._request("POST", "/api/task/offline_download/clear_done")
        return data.get("code") == 200

    async def get_raw_url(self, path: str) -> DirectLink:
        """Get direct/raw URL for a file (REQ-06: memory-only, not persisted).

        Tries fs/link first, falls back to fs/get on failure.
        """
        # Try fs/link first (OpenList ecosystem, not in official docs)
        try:
            data = await self._request("POST", "/api/fs/link", json={"path": path})
            url = data.get("data", {}).get("url", "")
            if url:
                return DirectLink(url=url)
        except (OpenListApiError, ExternalApiError) as e:
            logger.debug(f"[openlist] fs/link failed for {path}, trying fs/get: {e}")

        # Fallback to fs/get
        data = await self._request("POST", "/api/fs/get", json={"path": path})
        raw_url = data.get("data", {}).get("raw_url", "")
        if not raw_url:
            raise OpenListApiError(f"No raw_url in fs/get response for {path}")
        return DirectLink(url=raw_url)

    async def stat(self, path: str) -> NetFile | None:
        """Check if file/directory exists (probe for idempotency, REQ-07).

        Returns NetFile if exists, None if not found.
        Uses fs/get with path to check existence.
        """
        try:
            data = await self._request("POST", "/api/fs/get", json={"path": path})
            info = data.get("data", {})
            if not info:
                return None
            return NetFile(
                name=info.get("name", ""),
                size=int(info.get("size", 0)),
                is_dir=info.get("is_dir", False),
                modified=info.get("modified", ""),
                sign=info.get("sign", ""),
            )
        except OpenListApiError as e:
            # "not found" type errors -> return None
            # Exact error message varies by OpenList version; M5 will calibrate
            msg = (e.message or "").lower()
            if "not found" in msg or "not exist" in msg or "404" in msg:
                return None
            raise

    async def list_dir(self, path: str) -> list[NetFile]:
        """List directory contents with automatic pagination (REQ-13).

        Handles both new API (has_more/pages_total) and legacy API (total).
        per_page capped at 500 (OpenList limit).
        """
        all_files: list[NetFile] = []
        page = 1
        has_more = True
        while has_more:
            files, has_more = await self.list_dir_page(path, page)
            all_files.extend(files)
            page += 1
        return all_files

    async def list_dir_page(
        self, path: str, page: int, per_page: int = 200
    ) -> tuple[list[NetFile], bool]:
        """List one directory page; returns (files, has_more) (N4, FE-11).

        per_page capped at 500 (OpenList limit); has_more follows the new API
        (has_more flag) or the legacy total-based protocol.
        """
        per_page = max(1, min(int(per_page), 500))
        data = await self._request(
            "POST",
            "/api/fs/list",
            json={
                "path": path,
                "page": page,
                "per_page": per_page,
            },
        )

        content = data.get("data", {})
        items = content.get("content") or []  # null content for empty dirs
        files = [
            NetFile(
                name=item.get("name", ""),
                size=int(item.get("size", 0)),
                is_dir=item.get("is_dir", False),
                modified=item.get("modified", ""),
                sign=item.get("sign", ""),
            )
            for item in items
        ]

        if "has_more" in content:
            return files, bool(content.get("has_more", False))
        if "total" in content:
            total = int(content.get("total", 0))
            fetched = (page - 1) * per_page + len(files)
            return files, fetched < total
        return files, False

    async def mkdir(self, path: str) -> None:
        """Create directory (REQ-07: 405 treated as success)."""
        try:
            await self._request("POST", "/api/fs/mkdir", json={"path": path})
        except OpenListApiError as e:
            # 405 = directory already exists = success (REQ-07)
            if e.code == 405:
                logger.debug(f"[openlist] mkdir 405 (already exists): {path}")
                return
            raise

    async def rename(self, path: str, new_name: str) -> None:
        """Rename a file or directory.

        Args:
            path: Full path to the file/directory
            new_name: New name (not full path)
        """
        await self._request(
            "POST",
            "/api/fs/rename",
            json={
                "path": path,
                "name": new_name,
            },
        )

    async def remove(self, dir_path: str, names: list[str]) -> None:
        """Remove files or directories.

        Args:
            dir_path: Parent directory path
            names: List of file/directory names to remove
        """
        await self._request(
            "POST",
            "/api/fs/remove",
            json={
                "dir": dir_path,
                "names": names,
            },
        )

    async def move(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        """Move files or directories.

        Args:
            src_dir: Source directory path
            dst_dir: Destination directory path
            names: List of file/directory names to move
        """
        await self._request(
            "POST",
            "/api/fs/move",
            json={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
            },
        )

    async def copy(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        """Copy files or directories.

        Args:
            src_dir: Source directory path
            dst_dir: Destination directory path
            names: List of file/directory names to copy
        """
        await self._request(
            "POST",
            "/api/fs/copy",
            json={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
            },
        )

    async def remove_empty_dirs(self, src_dir: str, names: list[str]) -> None:
        """Remove empty directories only.

        Args:
            src_dir: Parent directory path
            names: List of directory names to check and remove if empty
        """
        await self._request(
            "POST",
            "/api/fs/remove_empty_directory",
            json={
                "src_dir": src_dir,
                "names": names,
            },
        )

    async def recursive_move(
        self, src_dir: str, dst_dir: str, names: list[str]
    ) -> None:
        """Recursively move files and directories.

        Args:
            src_dir: Source directory path
            dst_dir: Destination directory path
            names: List of file/directory names to move
        """
        await self._request(
            "POST",
            "/api/fs/recursive_move",
            json={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
            },
        )

    @property
    def capability(self) -> str:
        """Current capability state (UNKNOWN | OK | BROKEN)."""
        return self._capability

    @property
    def base_url(self) -> str:
        """Configured base URL (without credentials)."""
        return self._base_url
