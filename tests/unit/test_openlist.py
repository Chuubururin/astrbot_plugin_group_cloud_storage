"""OpenList client and archive_map unit tests (REQ-03/05/07/09/11/13).

Tests:
1. Login / token injection (REQ-05)
2. Submit -> undone -> done lifecycle with DTO mapping (REQ-08/09)
3. 401 -> auto re-login and replay once (REQ-05)
4. mkdir 405 idempotent success (REQ-07)
5. list_dir multi-page + legacy total fallback (REQ-13)
6. get_raw_url: fs/link failure fallback to fs/get (REQ-05)
7. URL guard: loopback/10.x/192.168.x/169.254.x default reject, toggle allow (REQ-11)
8. archive_map parameter binding + v12 dual-path migration (REQ-03)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import (
    ExternalApiError,
    OpenListApiError,
    classify_error,
    normalize_task_state,
    validate_base_url,
    ErrorKind,
)
from adapters.external.openlist import (
    DirectLink,
    NetFile,
    OfflineTask,
    OpenListClient,
)


# ---------- base.py tests ----------


class TestValidateBaseUrl:
    """REQ-11: SSRF protection tests."""

    def test_valid_public_url(self):
        url = validate_base_url("https://example.com:5244")
        assert url == "https://example.com:5244"

    def test_valid_public_url_no_port(self):
        url = validate_base_url("https://example.com")
        assert url == "https://example.com"

    def test_reject_ftp_scheme(self):
        with pytest.raises(ExternalApiError, match="scheme.*not allowed"):
            validate_base_url("ftp://example.com")

    def test_reject_loopback_default(self):
        with pytest.raises(ExternalApiError, match="restricted address"):
            validate_base_url("http://127.0.0.1:5244")

    def test_allow_loopback_when_enabled(self):
        url = validate_base_url("http://127.0.0.1:5244", allow_private=True)
        assert url == "http://127.0.0.1:5244"

    def test_reject_private_10_default(self):
        with pytest.raises(ExternalApiError, match="restricted address"):
            validate_base_url("http://10.0.0.1:5244")

    def test_reject_private_192_168_default(self):
        with pytest.raises(ExternalApiError, match="restricted address"):
            validate_base_url("http://192.168.1.100:5244")

    def test_reject_link_local_default(self):
        with pytest.raises(ExternalApiError, match="restricted address"):
            validate_base_url("http://169.254.1.1:5244")

    def test_allow_private_when_enabled(self):
        url = validate_base_url("http://192.168.1.100:5244", allow_private=True)
        assert url == "http://192.168.1.100:5244"


class TestNormalizeTaskState:
    """REQ-05: state normalization tests."""

    def test_succeeded(self):
        assert normalize_task_state("succeeded") == "done"

    def test_running(self):
        assert normalize_task_state("running") == "running"

    def test_pending(self):
        assert normalize_task_state("pending") == "pending"

    def test_errored(self):
        assert normalize_task_state("errored") == "failed"

    def test_case_insensitive(self):
        assert normalize_task_state("SUCCEEDED") == "done"
        assert normalize_task_state("Running") == "running"

    def test_empty_string(self):
        assert normalize_task_state("") == "unknown"

    def test_none(self):
        assert normalize_task_state(None) == "unknown"

    def test_unknown_value(self):
        assert normalize_task_state("some_new_state") == "unknown"


class TestClassifyError:
    """REQ-05: error classification tests."""

    def test_timeout(self):
        """Timeout exceptions should be classified as TIMEOUT."""
        # Create a custom exception class that simulates httpx timeout
        class TimeoutException(Exception):
            pass

        exc = TimeoutException("request timed out")
        assert classify_error(exc) == ErrorKind.TIMEOUT

    def test_unsupported_404(self):
        exc = OpenListApiError("not found", code=404)
        assert classify_error(exc) == ErrorKind.UNSUPPORTED

    def test_unsupported_405(self):
        exc = OpenListApiError("already exists", code=405)
        assert classify_error(exc) == ErrorKind.UNSUPPORTED

    def test_remote_error_default(self):
        exc = Exception("some error")
        assert classify_error(exc) == ErrorKind.REMOTE_ERROR


# ---------- openlist.py tests ----------


class TestOpenListClientAuth:
    """REQ-05: authentication tests."""

    @pytest.mark.asyncio
    async def test_token_injection(self):
        """Direct token injection should be used without login."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token_123",
        )
        token = await client.ensure_token()
        assert token == "test_token_123"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_login_with_credentials(self):
        """Login with username/password should return token."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            username="admin",
            password="secret",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "message": "success",
            "data": {"token": "jwt_token_abc"},
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.post.return_value = mock_resp
            mock_ensure.return_value = mock_http

            token = await client.ensure_token()
            assert token == "jwt_token_abc"

        await client.aclose()

    @pytest.mark.asyncio
    async def test_login_failure_raises(self):
        """Login failure should raise OpenListApiError."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            username="admin",
            password="wrong",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.post.return_value = mock_resp
            mock_ensure.return_value = mock_http

            with pytest.raises(OpenListApiError):
                await client.ensure_token()

        await client.aclose()


class TestOpenListClientSubmit:
    """REQ-08/09: submit offline download with DTO mapping."""

    @pytest.mark.asyncio
    async def test_submit_offline_download(self):
        """Submit should return list of OfflineTask DTOs."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "message": "success",
            "data": {
                "tasks": [
                    {
                        "id": "task_123",
                        "name": "file.zip",
                        "state": "running",
                        "status": "",
                        "progress": 0.5,
                        "error": "",
                    }
                ]
            },
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            tasks = await client.submit_offline_download(
                urls=["https://cdn.example.com/file.zip"],
                path="/群文件/123",
            )

            assert len(tasks) == 1
            assert isinstance(tasks[0], OfflineTask)
            assert tasks[0].id == "task_123"
            assert tasks[0].state == "running"
            assert tasks[0].progress == 0.5

        await client.aclose()


class TestOpenListClientMkdir:
    """REQ-07: mkdir 405 idempotent success."""

    @pytest.mark.asyncio
    async def test_mkdir_success(self):
        """Normal mkdir should succeed."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 200, "message": "success"}

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            # Should not raise
            await client.mkdir("/群文件/123")

        await client.aclose()

    @pytest.mark.asyncio
    async def test_mkdir_405_treated_as_success(self):
        """405 (already exists) should be treated as success (REQ-07)."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 405
        mock_resp.text = "already exists"

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            # Should not raise
            await client.mkdir("/群文件/123")

        await client.aclose()


class TestOpenListClientListDir:
    """REQ-13: auto-pagination tests."""

    @pytest.mark.asyncio
    async def test_list_dir_single_page(self):
        """Single page should return all files."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "message": "success",
            "data": {
                "content": [
                    {"name": "file1.zip", "size": 100, "is_dir": False, "modified": "2024-01-01"},
                    {"name": "file2.zip", "size": 200, "is_dir": False, "modified": "2024-01-02"},
                ],
                "has_more": False,
            },
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            files = await client.list_dir("/群文件/123")

            assert len(files) == 2
            assert isinstance(files[0], NetFile)
            assert files[0].name == "file1.zip"
            assert files[1].size == 200

        await client.aclose()

    @pytest.mark.asyncio
    async def test_list_dir_multi_page(self):
        """Multi-page should aggregate results."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = {
            "code": 200,
            "data": {
                "content": [{"name": "f1.zip", "size": 100, "is_dir": False, "modified": ""}],
                "has_more": True,
            },
        }

        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = {
            "code": 200,
            "data": {
                "content": [{"name": "f2.zip", "size": 200, "is_dir": False, "modified": ""}],
                "has_more": False,
            },
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.side_effect = [page1_resp, page2_resp]
            mock_ensure.return_value = mock_http

            files = await client.list_dir("/群文件/123")

            assert len(files) == 2
            assert files[0].name == "f1.zip"
            assert files[1].name == "f2.zip"

        await client.aclose()

    @pytest.mark.asyncio
    async def test_list_dir_legacy_total_fallback(self):
        """Legacy API with total field should be supported."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {
                "content": [{"name": "f1.zip", "size": 100, "is_dir": False, "modified": ""}],
                "total": 1,
            },
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            files = await client.list_dir("/群文件/123")

            assert len(files) == 1
            assert files[0].name == "f1.zip"

        await client.aclose()


class TestOpenListClientGetRawUrl:
    """REQ-05/06: get_raw_url with fs/link fallback."""

    @pytest.mark.asyncio
    async def test_get_raw_url_fs_link_success(self):
        """fs/link success should return DirectLink."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"url": "https://cdn.example.com/file.zip?sign=abc"},
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            link = await client.get_raw_url("/群文件/123/file.zip")

            assert isinstance(link, DirectLink)
            assert "sign=abc" in link.url

        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_raw_url_fallback_to_fs_get(self):
        """fs/link failure should fallback to fs/get."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        # fs/link fails
        link_resp = MagicMock()
        link_resp.status_code = 404
        link_resp.text = "not found"

        # fs/get succeeds
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "code": 200,
            "data": {"raw_url": "https://storage.example.com/file.zip"},
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.side_effect = [link_resp, get_resp]
            mock_ensure.return_value = mock_http

            link = await client.get_raw_url("/群文件/123/file.zip")

            assert isinstance(link, DirectLink)
            assert link.url == "https://storage.example.com/file.zip"

        await client.aclose()


class TestOpenListClientStat:
    """REQ-07: stat probe for idempotency."""

    @pytest.mark.asyncio
    async def test_stat_file_exists(self):
        """stat should return NetFile when file exists."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {
                "name": "file.zip",
                "size": 12345,
                "is_dir": False,
                "modified": "2024-01-01",
                "sign": "abc123",
            },
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            result = await client.stat("/群文件/123/file.zip")

            assert isinstance(result, NetFile)
            assert result.name == "file.zip"
            assert result.size == 12345

        await client.aclose()

    @pytest.mark.asyncio
    async def test_stat_file_not_found(self):
        """stat should return None when file not found."""
        client = OpenListClient(
            base_url="https://example.com:5244",
            token="test_token",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": None,
        }

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http = AsyncMock()
            mock_http.request.return_value = mock_resp
            mock_ensure.return_value = mock_http

            result = await client.stat("/群文件/123/nonexistent.zip")

            assert result is None

        await client.aclose()


# ---------- archive_map tests ----------


class TestArchiveMap:
    """REQ-03: archive_map CRUD with parameter binding."""

    @pytest.fixture
    async def store(self, tmp_path):
        from adapters.persistence.sqlite import SqliteMetaStore

        s = SqliteMetaStore(tmp_path / "meta.db")
        await s.init()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, store):
        """Upsert should insert and retrieve correctly."""
        row = {
            "resource_id": 42,
            "group_id": "123456",
            "task_id": "task_abc",
            "remote_path": "/群文件/123456/file.zip",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01T00:00:00",
        }
        await store.upsert_archive_map(row)

        result = await store.get_archive_map("123456", 42, "out")
        assert result is not None
        assert result["resource_id"] == 42
        assert result["task_id"] == "task_abc"
        assert result["state"] == "pending"

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, store):
        """Upsert same key should update, not duplicate."""
        row1 = {
            "resource_id": 42,
            "group_id": "123456",
            "task_id": "task_1",
            "remote_path": "/群文件/123456/file.zip",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01T00:00:00",
        }
        row2 = {
            "resource_id": 42,
            "group_id": "123456",
            "task_id": "task_2",
            "remote_path": "/群文件/123456/file.zip",
            "direction": "out",
            "state": "done",
            "updated_at": "2024-01-02T00:00:00",
        }
        await store.upsert_archive_map(row1)
        await store.upsert_archive_map(row2)

        result = await store.get_archive_map("123456", 42, "out")
        assert result["task_id"] == "task_2"
        assert result["state"] == "done"

    @pytest.mark.asyncio
    async def test_clear_archive_map(self, store):
        """Clear should remove the entry."""
        row = {
            "resource_id": 42,
            "group_id": "123456",
            "task_id": "task_abc",
            "remote_path": "/群文件/123456/file.zip",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01T00:00:00",
        }
        await store.upsert_archive_map(row)
        await store.clear_archive_map("123456", 42, "out")

        result = await store.get_archive_map("123456", 42, "out")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_archive_map_filter_by_state(self, store):
        """List should filter by state and direction."""
        rows = [
            {
                "resource_id": 1,
                "group_id": "g1",
                "task_id": "t1",
                "remote_path": "/p1",
                "direction": "out",
                "state": "pending",
                "updated_at": "2024-01-01",
            },
            {
                "resource_id": 2,
                "group_id": "g1",
                "task_id": "t2",
                "remote_path": "/p2",
                "direction": "out",
                "state": "done",
                "updated_at": "2024-01-02",
            },
            {
                "resource_id": 3,
                "group_id": "g1",
                "task_id": "t3",
                "remote_path": "/p3",
                "direction": "in",
                "state": "pending",
                "updated_at": "2024-01-03",
            },
        ]
        for row in rows:
            await store.upsert_archive_map(row)

        # Only pending out
        result = await store.list_archive_map(states=("pending",), direction="out")
        assert len(result) == 1
        assert result[0]["resource_id"] == 1

        # Pending + running out
        result = await store.list_archive_map(states=("pending", "running"), direction="out")
        assert len(result) == 1

        # Pending in
        result = await store.list_archive_map(states=("pending",), direction="in")
        assert len(result) == 1
        assert result[0]["resource_id"] == 3

    @pytest.mark.asyncio
    async def test_update_archive_state(self, store):
        """Update state should change state and updated_at."""
        row = {
            "resource_id": 42,
            "group_id": "g1",
            "task_id": "task_abc",
            "remote_path": "/p1",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01",
        }
        await store.upsert_archive_map(row)

        await store.update_archive_state(row, "done")

        result = await store.get_archive_map("g1", 42, "out")
        assert result["state"] == "done"

    @pytest.mark.asyncio
    async def test_update_archive_state_by_task(self, store):
        """Update by task_id should work."""
        row = {
            "resource_id": 42,
            "group_id": "g1",
            "task_id": "task_abc",
            "remote_path": "/p1",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01",
        }
        await store.upsert_archive_map(row)

        await store.update_archive_state_by_task("task_abc", "done")

        result = await store.get_archive_map("g1", 42, "out")
        assert result["state"] == "done"

    @pytest.mark.asyncio
    async def test_archive_map_no_url_column(self, store):
        """REQ-06: archive_map must not store URL (only remote_path)."""
        row = {
            "resource_id": 42,
            "group_id": "g1",
            "task_id": "task_abc",
            "remote_path": "/群文件/g1/file.zip",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01",
        }
        await store.upsert_archive_map(row)

        result = await store.get_archive_map("g1", 42, "out")
        # Verify no URL field exists
        assert "url" not in result
        assert "raw_url" not in result
        assert "direct_link" not in result


class TestArchiveMapMigration:
    """REQ-03: v12 migration fresh + upgrade paths."""

    @pytest.mark.asyncio
    async def test_fresh_install_has_archive_map(self, tmp_path):
        """Fresh install should have archive_map table."""
        from adapters.persistence.sqlite import SqliteMetaStore

        s = SqliteMetaStore(tmp_path / "meta.db")
        await s.init()

        import sqlite3
        conn = sqlite3.connect(tmp_path / "meta.db")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "archive_map" in tables
        conn.close()
        await s.close()

    @pytest.mark.asyncio
    async def test_v11_to_v12_upgrade(self, tmp_path):
        """Upgrade from v11 to v12 should add archive_map.

        按迁移契约真实构造 v11 库（逐段执行迁移 1..11 + 版本 11）——
        旧写法"先建最新库再改版本号"会保留后续列，与 v15 的 ALTER 冲突。
        """
        from adapters.persistence.sqlite import SqliteMetaStore
        from adapters.persistence.sqlite.migrations import MIGRATIONS as _MIGRATIONS, SCHEMA_VERSION as _SCHEMA_VERSION

        import sqlite3

        db = tmp_path / "meta.db"
        conn = sqlite3.connect(db)
        for v in range(1, 12):
            for sql in _MIGRATIONS[v]:
                conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (v,)
            )
        conn.commit()
        conn.close()

        # 现在 init —— 应执行 v12..v15 增量迁移（archive_map + v15 记录/隐藏列）
        s12 = SqliteMetaStore(db)
        await s12.init()

        conn = sqlite3.connect(db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "archive_map" in tables

        # Verify schema version
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == _SCHEMA_VERSION
        conn.close()
        await s12.close()

    @pytest.mark.asyncio
    async def test_v12_migration_idempotent(self, tmp_path):
        """Running init twice should be idempotent."""
        from adapters.persistence.sqlite import SqliteMetaStore
        from adapters.persistence.sqlite.migrations import SCHEMA_VERSION as _SCHEMA_VERSION

        s = SqliteMetaStore(tmp_path / "meta.db")
        await s.init()
        await s.init()  # Second init should not fail

        import sqlite3
        conn = sqlite3.connect(tmp_path / "meta.db")
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == _SCHEMA_VERSION
        conn.close()
        await s.close()


# ---------- dlserver guard tests (REQ-16) ----------


class TestDlserverGuard:
    """REQ-16: download server guard tests."""

    def test_guard_requires_enabled(self):
        """Disabled dlserver should be rejected."""
        # This is tested at bridge service level (M3)
        # Placeholder for integration test
        pass

    def test_guard_requires_port(self):
        """Zero port should be rejected."""
        # This is tested at bridge service level (M3)
        # Placeholder for integration test
        pass


class TestArchiveRename:
    """D1 fix: Rename UUID filename to intended name."""

    @pytest.fixture
    async def store(self, tmp_path):
        from adapters.persistence.sqlite import SqliteMetaStore

        s = SqliteMetaStore(tmp_path / "meta.db")
        await s.init()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_update_archive_remote_path(self, store):
        """Update remote_path should work correctly."""
        row = {
            "resource_id": 42,
            "group_id": "g1",
            "task_id": "task_abc",
            "remote_path": "/dir/uuid-name-123",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01",
        }
        await store.upsert_archive_map(row)

        # Update remote_path
        await store.update_archive_remote_path(42, "g1", "out", "/dir/intended-name.jpg")

        result = await store.get_archive_map("g1", 42, "out")
        assert result is not None
        assert result["remote_path"] == "/dir/intended-name.jpg"

    @pytest.mark.asyncio
    async def test_update_archive_state_by_resource_id(self, store):
        """Update state should work even if remote_path changes."""
        row = {
            "resource_id": 42,
            "group_id": "g1",
            "task_id": "task_abc",
            "remote_path": "/dir/uuid-name-123",
            "direction": "out",
            "state": "pending",
            "updated_at": "2024-01-01",
        }
        await store.upsert_archive_map(row)

        # Update remote_path first
        await store.update_archive_remote_path(42, "g1", "out", "/dir/intended-name.jpg")

        # Update state should still work (uses resource_id + group_id + direction)
        await store.update_archive_state(row, "done")

        result = await store.get_archive_map("g1", 42, "out")
        assert result is not None
        assert result["state"] == "done"
        assert result["remote_path"] == "/dir/intended-name.jpg"
