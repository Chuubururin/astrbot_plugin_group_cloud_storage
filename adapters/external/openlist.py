"""OpenListClient -- thin httpx client for the OpenList control plane.

Covers capability probing with degradation, URL resolution without
persistence, idempotent task operations, DTO mapping, and automatic
pagination.

Dependencies:
- httpx (host dependency, explicitly declared in requirements.txt)
- adapters/external/base.py (error types, SSRF protection, state normalization)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from core.log import logger

from .base import (
    ExternalApiError,
    OpenListApiError,
    classify_error,
    normalize_task_state,
    validate_base_url_structure,
    validate_hostname_dns,
)


# Wire DTOs and constants live in openlist_dto; re-exported here so every
# existing ``from ...openlist import X`` keeps working.
from .openlist_dto import (  # noqa: F401
    DirectLink,
    NetFile,
    OfflineTask,
    _MAX_LIST_PAGES,
    _STORAGE_MARKERS,
)

__all__ = [
    "DirectLink",
    "NetFile",
    "OfflineTask",
    "OpenListClient",
]

# aclose() logs out best-effort: bound it explicitly so a hung control plane
# cannot stall plugin unload for the client's full default timeout (30s).
_LOGOUT_TIMEOUT = 5.0


class OpenListClient:
    """Thin async client for OpenList REST API.

    Control-plane only: manages tasks, metadata, and file operations.
    No file content transfer (data-plane stays with dlserver/OpenList downloader).

    Features:
    - Lazy httpx.AsyncClient initialization
    - Automatic 401/403 re-login with single replay 
    - Envelope error handling
    - SSRF protection 
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
        # Validate the URL *shape* now (scheme / hostname / literal-IP range)
        # - pure parsing, no I/O, so a bad value still fails loudly here.
        #
        # The DNS half is deliberately deferred to _ensure_validated(): the
        # constructor runs from bootstrap.build_components, i.e. on AstrBot's
        # synchronous plugin-construction path, and a blocking getaddrinfo
        # there stalls plugin load for the resolver's whole timeout with no
        # event loop to yield to (W-3b).
        self._base_url = validate_base_url_structure(
            base_url, allow_private=allow_private_address
        )
        self._validated = False
        self._username = username
        self._password = password
        self._token = token
        self._timeout = timeout
        self._allow_private = allow_private_address

        # Lazy-initialized httpx client
        self._client: httpx.AsyncClient | None = None

        # Capability state 
        self._capability: str = "UNKNOWN"  # UNKNOWN | OK | BROKEN
        self._ping_failures: int = 0

    async def _ensure_validated(self) -> None:
        """Run the deferred DNS half of base-URL validation, once.

        Off the event loop via ``asyncio.to_thread`` because
        ``validate_hostname_dns`` resolves with a blocking ``getaddrinfo``
        (same convention as ``assert_fetch_url_allowed``).  Raises
        ``ExternalApiError`` on a restricted address or DNS failure, i.e. the
        exact error the constructor used to raise - now surfaced at first
        use instead of at plugin load (W-3b).
        """
        if self._validated:
            return
        await asyncio.to_thread(
            validate_hostname_dns,
            self._base_url,
            allow_private=self._allow_private,
        )
        self._validated = True

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy-create httpx.AsyncClient (after deferred validation)."""
        await self._ensure_validated()
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close httpx client and optionally logout."""
        if self._client is not None:
            # Best-effort logout, and only when a session actually exists:
            # _request_no_retry -> ensure_token() would otherwise perform a
            # *login* while we are tearing the client down. Bounded by
            # _LOGOUT_TIMEOUT so a hung control plane cannot stall unload.
            if self._token:
                try:
                    await self._request_no_retry(
                        "GET", "/api/auth/logout", timeout=_LOGOUT_TIMEOUT
                    )
                except Exception:
                    pass
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        """Health check .

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

        token = (data.get("data") or {}).get("token")
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
        timeout: float | None = None,
    ) -> httpx.Response:
        """Single request attempt without retry logic. Returns raw httpx.Response.

        ``timeout`` overrides the client default for this one call (None keeps
        the client default); teardown paths pass a short one.
        """
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

        try:
            # Only add the key when set: httpx treats an explicit
            # timeout=None as "no timeout at all", not "client default".
            extra: dict[str, Any] = {}
            if timeout is not None:
                extra["timeout"] = timeout
            resp = await client.request(
                method,
                path,
                json=json,
                params=params,
                headers=headers,
                **extra,
            )
        except httpx.TimeoutException as e:
            # httpx exception str is empty; name the kind so callers
            # surface a readable message instead of "failed: ".
            raise ExternalApiError(
                "openlist",
                f"timeout after client timeout ({type(e).__name__})",
            ) from e
        except httpx.HTTPError as e:
            kind = classify_error(e).value
            raise ExternalApiError(
                "openlist", f"{kind}: {type(e).__name__}: {e}"
            ) from e
        return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request with automatic 401/403 re-login .

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
                ) from e

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
            raise OpenListApiError(f"Invalid JSON response: {e}") from e

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
        """Submit offline download task .

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

        tasks_data = (data.get("data") or {}).get("tasks") or []
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=normalize_task_state(t.get("state", "")),
                status=t.get("status", ""),
                progress=float(t.get("progress", 0)),
                error=t.get("error", ""),
            )
            for t in tasks_data
        ]

    async def tasks_undone(self) -> list[OfflineTask]:
        """Get list of undone offline download tasks."""
        data = await self._request("GET", "/api/task/offline_download/undone")
        tasks_data = data.get("data") or []
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=normalize_task_state(t.get("state", "")),
                status=t.get("status", ""),
                progress=float(t.get("progress", 0)),
                error=t.get("error", ""),
            )
            for t in tasks_data
        ]

    async def tasks_done(self) -> list[OfflineTask]:
        """Get list of completed offline download tasks."""
        data = await self._request("GET", "/api/task/offline_download/done")
        tasks_data = data.get("data") or []
        return [
            OfflineTask(
                id=t.get("id", ""),
                name=t.get("name", ""),
                state=normalize_task_state(t.get("state", "")),
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

    async def get_raw_url(self, path: str) -> DirectLink:
        """Get direct/raw URL for a file .

        Tries fs/link first, falls back to fs/get on failure.
        """
        # Try fs/link first (OpenList ecosystem, not in official docs)
        try:
            data = await self._request("POST", "/api/fs/link", json={"path": path})
            url = (data.get("data") or {}).get("url") or ""
            if url:
                return DirectLink(url=url)
        except (OpenListApiError, ExternalApiError) as e:
            logger.debug(f"[openlist] fs/link failed for {path}, trying fs/get: {e}")

        # Fallback to fs/get
        data = await self._request("POST", "/api/fs/get", json={"path": path})
        raw_url = (data.get("data") or {}).get("raw_url") or ""
        if not raw_url:
            raise OpenListApiError(f"No raw_url in fs/get response for {path}")
        return DirectLink(url=raw_url)

    async def stat(self, path: str) -> NetFile | None:
        """Check if file/directory exists .

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
            msg = (e.message or "").lower()
            # An unmounted path reports "failed get storage: storage not
            # found" - which *contains* "not found", so the file-missing
            # branch below would swallow a mount-configuration error as
            # "no such file" (W-2).  It must surface: callers use None to
            # mean "safe to create/re-submit", and a bad mount would then
            # look like a healthy transfer target (or, in recovery, get the
            # task marked FAILED and the user notified).
            if any(marker in msg for marker in _STORAGE_MARKERS):
                raise
            # "not found" style errors -> return None
            # Exact wording varies across OpenList versions; match common variants.
            if "not found" in msg or "not exist" in msg or "404" in msg:
                return None
            raise

    async def probe_mount(self, path: str) -> tuple[bool, str | None]:
        """Does ``path`` resolve to an OpenList storage mount?

        OpenList matches paths against its storages by longest prefix; a path
        outside every mount fails with "failed get storage: storage not
        found", and ``mkdir`` cannot create a mount point - so a destination
        root outside a mount breaks every transfer permanently (W-1).

        Not built on :meth:`stat` on purpose: ``stat`` classifies by substring
        and the unmounted text also contains "not found", so it returns None
        for both "no mount" and "no file" (defect W-2).  The mount error is
        matched explicitly here.  Returns ``(True, None)`` when a mount owns
        the path (its existence is irrelevant), ``(False, reason)`` otherwise.
        Advisory probe, used on the startup path; network errors propagate.
        """
        try:
            await self._request("POST", "/api/fs/get", json={"path": path})
        except OpenListApiError as e:
            msg = (e.message or "").lower()
            if any(marker in msg for marker in _STORAGE_MARKERS):
                return False, (e.message or "storage not found")
            if "not found" in msg or "not exist" in msg or "404" in msg:
                return True, None  # mounted, that one object is just absent
            raise
        return True, None  # fs/get answered, so a mount owns this path

    async def list_dir(self, path: str) -> list[NetFile]:
        """List directory contents with automatic pagination .

        Handles both new API (has_more/pages_total) and legacy API (total).
        per_page capped at 500 (OpenList limit).
        """
        all_files: list[NetFile] = []
        page = 1
        has_more = True
        while has_more:
            files, has_more = await self.list_dir_page(path, page)
            all_files.extend(files)
            if not files:
                # An empty page means has_more cannot be trusted: a server
                # answering has_more=true forever would spin here and grow
                # all_files without bound.
                break
            page += 1
            if page > _MAX_LIST_PAGES:
                logger.warning(
                    f"[openlist] list_dir({path}) stopped after "
                    f"{_MAX_LIST_PAGES} pages with has_more still set"
                )
                break
        return all_files

    async def list_dir_page(
        self, path: str, page: int, per_page: int = 200
    ) -> tuple[list[NetFile], bool]:
        """List one directory page; returns (files, has_more) .

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

        content = data.get("data") or {}
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
        """Create directory ."""
        try:
            await self._request("POST", "/api/fs/mkdir", json={"path": path})
        except OpenListApiError as e:
            # 405 = directory already exists = success 
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
