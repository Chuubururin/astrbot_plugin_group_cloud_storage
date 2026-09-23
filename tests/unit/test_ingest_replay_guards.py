"""回归测试：ingest 重入（retry / pause -> resume）去重守卫（R-2 / G-1..G-4）。

背景
----
OpQueue 在 **重试** 与 **暂停→恢复** 两条路径上都会把 handler 从第一行重新
执行（`execution.py` 的 `op.replayed = True`）。凡是「重入会产生重复远程副作用」
的 handler 都必须自带判重，否则云端出现同名重复对象。

已有守卫（正确实现，本文件只做回归钉住）：
- `image_album` / `video_album`：`_is_replay` + `_album_has_media`（album.py）
- `video_upload` 分片分支：持久化 `volumes.status == "uploaded"` 判据
- `essence_save`：payload `sent_parts` 判据 + `checkpoint_payload`

本文件覆盖的缺口（本次修复）：
- G-1 `video_upload` 直传分支：重放会重复 `upload_group_file`
- G-2 `fetch` -> 相册图片：重放会重复 `upload_image_to_qun_album`
- G-3 `fetch` -> 群文件：重放会重复 `upload_group_file`
- G-4 `fetch` -> essence / video_album：重放会重复派生任务（新 task_id）

守卫判据是「仅重放时执行的远程探针」（与 album.py 同构），因此
happy path（`replayed=False`）必须**不产生额外列举调用**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.ingest.video import album_has_media, remote_has_file  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402

_SRC_BYTES = 128


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ingest: CloudIngestService | None = None
    queue = OpQueue(lambda op: ingest.handle(op), backoff_base=0.05)
    await queue.start()
    ingest = CloudIngestService(
        api,
        store,
        queue,
        sync,
        tmp_dir=tmp_path / "tmp",
        config={
            "essence_chunk_size": 1000,
            "video_segment_seconds": 600,
            "fetch_max_bytes": 10 * 1024 * 1024,
            "fetch_timeout_sec": 10,
        },
    )

    async def _fake_download(self, url: str, dest: Path) -> int:
        dest.write_bytes(b"P" * _SRC_BYTES)
        return _SRC_BYTES

    monkeypatch.setattr(CloudIngestService, "_download", _fake_download)
    yield tmp_path, store, api, queue, ingest
    await queue.shutdown()
    await store.close()


def _op(tmp_path, kind, name, replayed, staged="src.bin", **extra):
    """构造 op。`staged` 大小固定为 _SRC_BYTES，便于探针按 size 命中。"""
    path = tmp_path / staged
    path.write_bytes(b"P" * _SRC_BYTES)
    payload = {"path": path.as_posix(), "name": name}
    payload.update(extra)
    return SimpleNamespace(
        task_id="t1",
        kind=kind,
        target="g1",
        cancel=False,
        pause=False,
        retries=0,
        replayed=replayed,
        payload=payload,
    )


def _fm(api, name: str) -> list[str]:
    """该名字对应的 upload_group_file 调用次数。"""
    return [c for c in api.calls if c == f"upload_group_file:g1:{name}"]


def _probe_spy(monkeypatch, module, calls: list):
    """把 module 里的远程探针换成记录调用的替身。

    不能用 api.calls 里的列举计数来判定「探针是否被执行」：上传成功后
    `sync.run_full_sync` 自己也会 `list_group_root`，两者混在一起分不开。
    """
    async def _spy(*args, **kwargs):
        calls.append("probe")
        return False

    monkeypatch.setattr(module, "remote_has_file", _spy)


def _fixed(sec):
    async def _probe(self, path):
        return sec

    return _probe


# =============================== 探针单测 ===============================


@pytest.mark.asyncio
async def test_remote_has_file_matches_by_name_and_size(env):
    """探针按 name 匹配；给了 size 就必须同时命中 size。"""
    _, _, api, _, _ = env
    api.tree = {
        None: ([{"file_id": "f1", "busid": 0, "name": "v.mp4", "size": 100}], [])
    }
    assert await remote_has_file(api, "g1", "v.mp4") is True
    assert await remote_has_file(api, "g1", "v.mp4", 100) is True
    assert await remote_has_file(api, "g1", "v.mp4", 999) is False, "size 不符不得命中"
    assert await remote_has_file(api, "g1", "other.mp4") is False
    # 带目录前缀：按 basename 比较（与 album.py 的 Path(name).name 一致）
    assert await remote_has_file(api, "g1", "/tmp/x/v.mp4") is True


@pytest.mark.asyncio
async def test_remote_has_file_degrades_to_false_on_listing_failure(env):
    """列举失败（无探针通道）→ False，即回退到「照常上传」，不吞掉上传。"""
    _, _, api, _, _ = env

    async def _boom(group_id):
        raise RuntimeError("no probe channel")

    api.list_group_root = _boom
    assert await remote_has_file(api, "g1", "v.mp4") is False


@pytest.mark.asyncio
async def test_album_has_media_matches_desc_name_and_file_name(env):
    """相册探针接受 desc / name / file_name 三种键（与 album.py 判据等价）。"""
    _, _, api, _, _ = env
    api.album_media = {
        "g1:a1": [
            {"desc": "pic.png"},
            {"name": "pic2.png"},
            {"file_name": "pic3.png"},
        ]
    }
    assert await album_has_media(api, "g1", "a1", "pic.png") is True
    assert await album_has_media(api, "g1", "a1", "pic2.png") is True
    assert await album_has_media(api, "g1", "a1", "pic3.png") is True
    assert await album_has_media(api, "g1", "a1", "nope.png") is False


# =============================== G-1 ===============================


@pytest.mark.asyncio
async def test_g1_video_direct_replay_skips_reupload(env, monkeypatch):
    """G-1：直传分支重放时云端已有同名同大小视频 → 不得再上传一次。"""
    tmp_path, _, api, _, ingest = env
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed(10))
    api.tree = {
        None: (
            [{"file_id": "f1", "busid": 0, "name": "v.mp4", "size": _SRC_BYTES}],
            [],
        )
    }
    await ingest._do_video_upload(_op(tmp_path, "video_upload", "v.mp4", True, "v.mp4"))
    assert _fm(api, "v.mp4") == [], "直传重放判重失效：视频被二次上传"


@pytest.mark.asyncio
async def test_g1_video_direct_fresh_run_uploads_without_probe(env, monkeypatch):
    """G-1 对侧：首跑必须正常上传，且不得为守卫执行探针（零额外往返）。"""
    tmp_path, _, api, _, ingest = env
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed(10))
    api.tree = {
        None: (
            [{"file_id": "f1", "busid": 0, "name": "v.mp4", "size": _SRC_BYTES}],
            [],
        )
    }
    probe_calls: list = []
    import core.application.ingest.video as video_mod

    _probe_spy(monkeypatch, video_mod, probe_calls)
    await ingest._do_video_upload(_op(tmp_path, "video_upload", "v.mp4", False, "v.mp4"))
    assert len(_fm(api, "v.mp4")) == 1, "首跑必须上传一次"
    assert probe_calls == [], "happy path 不得为守卫执行探针"


@pytest.mark.asyncio
async def test_g1_video_direct_replay_without_cloud_copy_still_uploads(env, monkeypatch):
    """G-1 边界：重放但云端没有该文件（首跑其实没落地）→ 必须照常上传，不能漏传。"""
    tmp_path, _, api, _, ingest = env
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed(10))
    await ingest._do_video_upload(_op(tmp_path, "video_upload", "v.mp4", True, "v.mp4"))
    assert len(_fm(api, "v.mp4")) == 1, "云端没有时必须补传"


@pytest.mark.asyncio
async def test_g1_video_direct_replay_size_mismatch_still_uploads(env, monkeypatch):
    """G-1 边界：同名但大小不同 = 另一个文件 → 不得误判为已上传。"""
    tmp_path, _, api, _, ingest = env
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed(10))
    api.tree = {
        None: ([{"file_id": "f1", "busid": 0, "name": "v.mp4", "size": 9999}], [])
    }
    await ingest._do_video_upload(_op(tmp_path, "video_upload", "v.mp4", True, "v.mp4"))
    assert len(_fm(api, "v.mp4")) == 1, "size 不符必须补传"


# =============================== G-3 ===============================


@pytest.mark.asyncio
async def test_g3_fetch_to_group_file_replay_skips_reupload(env):
    """G-3：fetch 落到群文件，重放时云端已有同名文件 → 不得再上传一次。"""
    tmp_path, _, api, _, ingest = env
    api.tree = {None: ([{"file_id": "f1", "busid": 0, "name": "a.bin", "size": 0}], [])}
    await ingest._do_fetch(_op(tmp_path, "fetch", "a.bin", True, url="http://x/a.bin"))
    assert _fm(api, "a.bin") == [], "fetch→群文件重放判重失效"


@pytest.mark.asyncio
async def test_g3_fetch_to_group_file_fresh_run_uploads_without_probe(env, monkeypatch):
    """G-3 对侧：首跑必须上传，且不得为守卫执行探针。"""
    tmp_path, _, api, _, ingest = env
    api.tree = {None: ([{"file_id": "f1", "busid": 0, "name": "a.bin", "size": 0}], [])}
    probe_calls: list = []
    import core.application.ingest.fetch as fetch_mod

    _probe_spy(monkeypatch, fetch_mod, probe_calls)
    await ingest._do_fetch(_op(tmp_path, "fetch", "a.bin", False, url="http://x/a.bin"))
    assert len(_fm(api, "a.bin")) == 1
    assert probe_calls == [], "happy path 不得为守卫执行探针"


# =============================== G-2 ===============================


@pytest.mark.asyncio
async def test_g2_fetch_to_album_image_replay_skips_reupload(env):
    """G-2：fetch 图片入相册，重放时相册已有同名媒体 → 不得再上传一次。"""
    tmp_path, _, api, _, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {"g1:a1": [{"desc": "pic.png"}]}
    await ingest._do_fetch(
        _op(
            tmp_path,
            "fetch",
            "pic.png",
            True,
            url="http://x/pic.png",
            to_album=True,
            album_name="测试相册",
        )
    )
    assert api.album_uploads == [], "fetch→相册重放判重失效"


@pytest.mark.asyncio
async def test_g2_fetch_to_album_image_fresh_run_uploads(env):
    """G-2 对侧：首跑必须入册一次。"""
    tmp_path, _, api, _, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {"g1:a1": [{"desc": "pic.png"}]}
    await ingest._do_fetch(
        _op(
            tmp_path,
            "fetch",
            "pic.png",
            False,
            url="http://x/pic.png",
            to_album=True,
            album_name="测试相册",
        )
    )
    assert len(api.album_uploads) == 1, "首跑必须入册一次"


# =============================== G-4 ===============================


@pytest.mark.asyncio
async def test_g4_fetch_to_essence_replay_skips_derived_submit(env, monkeypatch):
    """G-4：fetch→essence 的重放不得派生第二个 essence_save 任务。

    旧行为：每次重放都 `submit_essence_save` 出一个新 task_id，同一份文档在群里
    被发送两次，而首批 message_id 只存在于被派生的任务 payload 里。
    """
    tmp_path, _, _, queue, ingest = env
    submitted: list[str] = []
    real_submit = queue.submit

    async def _spy(kind, **kwargs):
        submitted.append(kind)
        return await real_submit(kind, **kwargs)

    monkeypatch.setattr(queue, "submit", _spy)
    await ingest._do_fetch(
        _op(tmp_path, "fetch", "doc.txt", True, url="http://x/doc.txt", to_essence=True)
    )
    assert "essence_save" not in submitted, "重放派生出了重复的 essence_save 任务"


@pytest.mark.asyncio
async def test_g4_fetch_to_essence_fresh_run_does_submit(env, monkeypatch):
    """G-4 对侧：首跑必须派生 essence_save。"""
    tmp_path, _, _, queue, ingest = env
    submitted: list[str] = []
    real_submit = queue.submit

    async def _spy(kind, **kwargs):
        submitted.append(kind)
        return await real_submit(kind, **kwargs)

    monkeypatch.setattr(queue, "submit", _spy)
    await ingest._do_fetch(
        _op(tmp_path, "fetch", "doc.txt", False, url="http://x/doc.txt", to_essence=True)
    )
    assert "essence_save" in submitted, "首跑必须派生 essence_save"


@pytest.mark.asyncio
async def test_g4b_fetch_to_album_video_replay_skips_submit_and_cleans_staged(
    env, monkeypatch
):
    """G-4b：fetch 视频入相册的重放不得派生第二个 video_album 任务，
    且被移出 *staged* 的临时文件必须就地清理（否则 tmp_dir 泄漏）。"""
    tmp_path, _, _, queue, ingest = env
    submitted: list[str] = []
    real_submit = queue.submit

    async def _spy(kind, **kwargs):
        submitted.append(kind)
        return await real_submit(kind, **kwargs)

    monkeypatch.setattr(queue, "submit", _spy)
    tmp = tmp_path / "tmp"
    before = set(tmp.glob("fetch_video_*")) if tmp.exists() else set()
    await ingest._do_fetch(
        _op(
            tmp_path,
            "fetch",
            "v.mp4",
            True,
            url="http://x/v.mp4",
            to_album=True,
            album_name="测试相册",
        )
    )
    assert "video_album" not in submitted, "重放派生出了重复的 video_album 任务"
    after = set(tmp.glob("fetch_video_*")) if tmp.exists() else set()
    assert after == before, "重放跳过派生后，暂存视频必须被清理（不得泄漏）"


@pytest.mark.asyncio
async def test_g4b_fetch_to_album_video_fresh_run_derives_task(env, monkeypatch):
    """G-4b 对侧：首跑必须派生 video_album。"""
    tmp_path, _, _, queue, ingest = env
    submitted: list[str] = []
    real_submit = queue.submit

    async def _spy(kind, **kwargs):
        submitted.append(kind)
        return await real_submit(kind, **kwargs)

    monkeypatch.setattr(queue, "submit", _spy)
    await ingest._do_fetch(
        _op(
            tmp_path,
            "fetch",
            "v.mp4",
            False,
            url="http://x/v.mp4",
            to_album=True,
            album_name="测试相册",
        )
    )
    assert "video_album" in submitted, "首跑必须派生 video_album"


# =============================== G-5 ===============================


def _folder_op(tmp_path, name, replayed, parent_id="/"):
    """create_folder 的 op（不需要 staged 文件）。"""
    return SimpleNamespace(
        task_id="t1",
        kind="create_folder",
        target="g1",
        cancel=False,
        pause=False,
        retries=0,
        replayed=replayed,
        payload={"name": name, "parent_id": parent_id},
    )


def _folder_service(api):
    """最小 duck-typed FileOpsService：只装 _do_create_folder 需要的东西。"""
    from core.application.files.folder import FolderMixin

    class _Svc(FolderMixin):
        pass

    svc = _Svc()
    svc.api = api
    svc._sync_locks = {}
    sync_calls: list = []

    class _Sync:
        async def run_full_sync(self, group_id, lock):
            sync_calls.append(group_id)
            return SimpleNamespace(ok=True, error=None)

    svc.sync = _Sync()
    return svc, sync_calls


def _folder_creates(api, name: str) -> list[str]:
    return [c for c in api.calls if c == f"create_group_file_folder:g1:{name}:/"]


@pytest.mark.asyncio
async def test_folder_exists_matches_root_and_subfolder(env):
    """文件夹探针：根目录 / 子目录都能命中，且按 strip 后比较。"""
    _, _, api, _, _ = env
    from core.application.files.folder import folder_exists

    api.tree = {None: ([], [{"folder_id": "d1", "name": "Docs"}])}
    assert await folder_exists(api, "g1", "Docs") is True
    assert await folder_exists(api, "g1", " Docs ") is True
    assert await folder_exists(api, "g1", "Nope") is False

    api.tree = {
        "/d1": ([], [{"folder_id": "d2", "name": "Inner"}]),
    }
    assert await folder_exists(api, "g1", "Inner", "/d1") is True
    assert await folder_exists(api, "g1", "Inner") is False, "根目录不应命中子目录项"


@pytest.mark.asyncio
async def test_folder_exists_degrades_to_false_on_listing_failure(env):
    """列举失败（无探针通道）→ False，即照常创建，不吞掉创建。"""
    _, _, api, _, _ = env
    from core.application.files.folder import folder_exists

    async def _boom(group_id):
        raise RuntimeError("no probe channel")

    api.list_group_root = _boom
    assert await folder_exists(api, "g1", "Docs") is False


@pytest.mark.asyncio
async def test_g5_create_folder_replay_skips_recreate(env):
    """G-5：重放时群里已有同名文件夹 → 不得再创建第二个（创建非幂等）。"""
    tmp_path, _, api, _, _ = env
    api.tree = {None: ([], [{"folder_id": "d1", "name": "Docs"}])}
    svc, _ = _folder_service(api)
    await svc._do_create_folder(_folder_op(tmp_path, "Docs", True))
    assert _folder_creates(api, "Docs") == [], "重放判重失效：创建了第二个同名文件夹"


@pytest.mark.asyncio
async def test_g5_create_folder_fresh_run_creates(env):
    """G-5 对侧：首跑必须创建（即使同名已存在，首跑也不该被守卫拦住）。"""
    tmp_path, _, api, _, _ = env
    api.tree = {None: ([], [{"folder_id": "d1", "name": "Docs"}])}
    svc, _ = _folder_service(api)
    await svc._do_create_folder(_folder_op(tmp_path, "Docs", False))
    assert len(_folder_creates(api, "Docs")) == 1, "首跑必须创建"


@pytest.mark.asyncio
async def test_g5_create_folder_replay_without_existing_still_creates(env):
    """G-5 边界：重放但群里没有该文件夹（首跑其实没落地）→ 必须补建。"""
    tmp_path, _, api, _, _ = env
    svc, _ = _folder_service(api)
    await svc._do_create_folder(_folder_op(tmp_path, "Docs", True))
    assert len(_folder_creates(api, "Docs")) == 1, "云端没有时必须补建"
