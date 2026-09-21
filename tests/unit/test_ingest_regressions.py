"""回归测试：云导入（ingest）路径。

- H1 直传上传失败（可重试）必须保留暂存源文件，否则重放必然失败
- M9 直传成功 / 终态失败后必须清理暂存源文件
- M13 分段上传重试时不得重传已上传的分片
- M1 精华分片发送不是幂等的：暂停恢复重放不得重发分片
- M2 相册判重必须用重放判据（op.replayed），不是 op.retries
- M7 to_essence 体积超限是静态条件：一次终态失败，不重下 4 次
- M8 相册终态拒绝（无相册且无创建接口）必须一并清理暂存文件
- 低危 fetch.py 取名字长度校验 / to_essence 读文件有上限且 offload 到线程
- 低危 essence.py 两个 handler 循环内补 pause_check
- 低危 兜底常量必须钉在 QQ 硬限上，且 service.py 的 fallback 真的生效
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.queue.op import OpPausedError  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from core.domain.enums import OneBotApiError, OneBotErrorKind  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from tests.contract.helpers import drain_op  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ingest: CloudIngestService | None = None
    queue = OpQueue(lambda op: ingest.handle(op), interval=0.0, backoff_base=1.0)
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
    monkeypatch.setattr(CloudIngestService, "_download", _fake_download)
    yield tmp_path, store, api, queue, ingest
    await queue.shutdown()
    await store.close()


async def _fake_download(self, url: str, dest: Path) -> int:
    data = b"HELLO-FROM-" + url.encode() * 64
    dest.write_bytes(data)
    return len(data)


def _fixed_duration(sec):
    async def _probe(self, path):
        return sec

    return _probe


async def _wait_terminal(queue, task_id, timeout: float = 30.0):
    """Wait for the newest queue row of this op to reach a terminal state.

    drain_op only returns the newest row, so it must not be used across a
    pause: the pause hold writes its own (non-terminal) row first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [
            r for r in (await queue.status())["recent"] if r["task_id"] == task_id
        ]
        if rows and rows[0]["state"] in ("ok", "failed", "cancelled"):
            return rows[0]
        await asyncio.sleep(0.02)
    raise TimeoutError(f"op {task_id} did not reach a terminal state")


# ---------- M9 ----------


@pytest.mark.asyncio
async def test_video_direct_upload_removes_staged_source(env, monkeypatch):
    """M9：直传（常态路径）返回前必须删掉暂存视频。"""
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(
        CloudIngestService, "_probe_duration", _fixed_duration(120)
    )
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "short.mp4")
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "ok"
    assert "upload_group_file:g1:short.mp4" in api.calls
    assert not src.exists(), "直传后暂存源文件必须被清理"


@pytest.mark.asyncio
async def test_video_direct_upload_removes_staged_source_on_terminal_failure(
    env, monkeypatch
):
    """M9/H1：直传抛不可重试错误时是终态，暂存文件必须清理（不留 tmp 垃圾）。"""
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(
        CloudIngestService, "_probe_duration", _fixed_duration(120)
    )

    async def _boom(*args, **kwargs):
        raise OneBotApiError(
            OneBotErrorKind.LOCAL_ERROR, "upload_group_file", "boom"
        )

    monkeypatch.setattr(api, "upload_group_file", _boom)
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "short.mp4")
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "failed"
    assert not src.exists(), "失败路径下暂存源文件也必须被清理"


# ---------- H1 ----------


@pytest.mark.asyncio
async def test_video_direct_retriable_failure_keeps_source_for_replay(env, monkeypatch):
    """H1：直传抛可重试错误（TIMEOUT）时必须保留暂存源文件。

    修复前 finally 无条件 unlink：队列重放时 src.exists() 为假 → 命中同文件顶部
    的「暂存文件已不存在」LOCAL_ERROR（不可重试）→ 3 次重试预算作废、用户视频
    丢失，错误文案「上传可能已完成」还与事实相反。分片分支特意保留源文件，直传
    分支却删掉，两个分支对重试的契约自相矛盾。
    """
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(120))
    attempts: list[str] = []
    real_upload = api.upload_group_file

    async def _flaky(
        group_id, file_path, name="", folder_id=None, folder="", upload_file=True
    ):
        attempts.append(name)
        if len(attempts) == 1:
            raise OneBotApiError(
                OneBotErrorKind.TIMEOUT, "upload_group_file", "transient"
            )
        await real_upload(group_id, file_path, name, folder_id=folder_id)

    monkeypatch.setattr(api, "upload_group_file", _flaky)
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "short.mp4")
    r = await _wait_terminal(queue, tid)
    assert r["state"] == "ok", r
    assert attempts == ["short.mp4", "short.mp4"], "重放没有重传（源文件被提前删除）"
    assert not src.exists(), "上传成功后才清理暂存文件"


# ---------- M13 ----------


@pytest.mark.asyncio
async def test_video_split_retry_skips_uploaded_parts(env, monkeypatch):
    """M13：队列重试不得把已上传的分片再传一遍（QQ 侧会出现同名重复分片）。"""
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "long.mp4"
    src.write_bytes(b"fakemp4" * 100)
    monkeypatch.setattr(
        CloudIngestService, "_probe_duration", _fixed_duration(1300)
    )

    async def _fake_split(srcp, out_dir, stem, max_sec):
        segs = []
        for i in range(1, 4):
            seg = out_dir / f"{stem}_seg{i:03d}.mp4"
            seg.write_bytes(f"SEG{i}".encode() * 100)
            segs.append(seg)
        return sorted(segs)

    import core.application.ingest.video as video_mod

    monkeypatch.setattr(video_mod, "split_video", _fake_split)

    attempts: list[str] = []
    real_upload = api.upload_group_file

    async def _flaky(group_id, file_path, name="", folder_id=None, folder="", upload_file=True):
        if name.startswith("long.part"):
            attempts.append(name)
            if len(attempts) == 2:  # 第 2 个分片瞬时失败 -> 队列重试
                raise OneBotApiError(
                    OneBotErrorKind.REMOTE_ERROR, "upload_group_file", "transient"
                )
        await real_upload(group_id, file_path, name, folder_id=folder_id)

    monkeypatch.setattr(api, "upload_group_file", _flaky)
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "long.mp4")
    r = await drain_op(queue, tid, timeout=60)
    assert r["state"] == "ok"
    assert attempts == [
        "long.part01.mp4",
        "long.part02.mp4",
        "long.part02.mp4",  # 只重传失败的那个
        "long.part03.mp4",
    ], attempts
    # 分片行仍为 3 条且都是 uploaded（没有重复行/重复上传）
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file"))
    parent = next(it for it in page.items if (it.meta or {}).get("kind") == "video")
    vols = await store.list_volumes(parent.resource_id)
    assert [v.seq for v in vols] == [1, 2, 3]
    assert all(v.status == "uploaded" for v in vols)


# ---------- M1 ----------


@pytest.mark.asyncio
async def test_essence_save_pause_resume_does_not_resend_parts(env, monkeypatch):
    """M1：暂停 → 恢复重跑 handler 时，已发送的精华分片不得重发。

    分片「发送 + 设精」不是幂等的：重发会让群内出现重复精华消息，且已发送片的
    message_id 只存在内存 parts 里，第二次运行以新的 source_ref 建行，前一批消息
    成为插件无法删除的孤儿。已发送 parts 写回 op.payload，重入时跳过。
    """
    tmp_path, store, api, queue, ingest = env
    text = "\n".join("字" * 500 for _ in range(4))
    real_pause_check = queue.pause_check
    calls = {"n": 0}

    async def _spy(op):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 1 片已发出，第 2 片发送前暂停
            op.pause = True
        await real_pause_check(op)

    monkeypatch.setattr(queue, "pause_check", _spy)
    tid = await ingest.submit_essence_save("g1", "长文归档", text)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and tid not in queue._paused:
        await asyncio.sleep(0.02)
    assert tid in queue._paused, "暂停未生效（检查点没被触发）"
    assert queue.resume_task(tid) == "resumed"
    r = await _wait_terminal(queue, tid)
    assert r["state"] == "ok", r

    markers = []
    for m in api.sent_messages:
        hit = re.findall(r"\[云盘\|[^\]]+\]", m["text"])
        assert len(hit) == 1, m["text"]
        markers.append(hit[0])
    assert len(markers) == len(set(markers)), f"重放重发了分片：{markers}"
    # 云端精华条目与本地 parts 行必须与已发送分片一一对应
    assert len(api.essences["g1"]) == len(markers)
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    parts = page.items[0].meta["parts"]
    assert [p["seq"] for p in parts] == list(range(1, len(markers) + 1))
    assert len({p["message_id"] for p in parts}) == len(markers)


# ---------- 低危：fetch 名字长度 ----------


@pytest.mark.asyncio
async def test_fetch_long_url_name_is_clamped(env):
    """URL 尾段超 80 字符时必须截断，而不是下载完才失败。"""
    tmp_path, store, api, queue, ingest = env
    long_name = "x" * 200 + ".txt"
    tid = await ingest.submit_fetch(
        "g1", f"https://example.com/dir/{long_name}", to_essence=True
    )
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "ok"
    for _ in range(200):
        if api.sent_messages:
            break
        await asyncio.sleep(0.05)
    assert api.sent_messages, "精华分片未发送"
    title = api.sent_messages[0]["text"].split("[云盘|")[1].split("|")[0]
    assert 0 < len(title) <= 80, len(title)
    assert title.endswith(".txt")


# ---------- 低危：fetch to_essence 读取上限 + offload ----------


@pytest.mark.asyncio
async def test_fetch_essence_text_read_offloaded_and_bounded(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    import core.application.ingest.fetch as fetch_mod

    submitted: list[tuple[str, str, int]] = []

    async def _fake_submit(group_id, title, text):
        submitted.append((group_id, title, len(text)))
        return "t1"

    monkeypatch.setattr(ingest, "submit_essence_save", _fake_submit)

    main_thread = threading.get_ident()
    seen: dict = {}
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        seen["thread"] = threading.get_ident()
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    op = SimpleNamespace(
        task_id="t1",
        kind="fetch",
        target="g1",
        cancel=False,
        pause=False,
        payload={
            "url": "https://example.com/doc.txt",
            "name": "doc.txt",
            "to_essence": True,
        },
    )
    await ingest._do_fetch(op)
    assert submitted and submitted[0][1] == "doc.txt"
    assert seen.get("thread") not in (None, main_thread), (
        "to_essence 的整文件读取必须 offload 到线程"
    )

    # 超限拒绝：不把整份下载（上限默认 2GB）读进内存
    monkeypatch.setattr(fetch_mod, "_ESSENCE_TEXT_MAX_BYTES", 4)
    op2 = SimpleNamespace(
        task_id="t2",
        kind="fetch",
        target="g1",
        cancel=False,
        pause=False,
        payload={
            "url": "https://example.com/doc2.txt",
            "name": "doc2.txt",
            "to_essence": True,
        },
    )
    # M7：静态条件必须抛 LOCAL_ERROR（不可重试），否则队列会把 32MiB+ 文档重下 4 次
    with pytest.raises(OneBotApiError, match="essence text source exceeds") as ei:
        await ingest._do_fetch(op2)
    assert ei.value.kind is OneBotErrorKind.LOCAL_ERROR
    assert not list((tmp_path / "tmp").glob("fetch_*.tmp")), "暂存文件已清理"


@pytest.mark.asyncio
async def test_fetch_essence_oversize_is_terminal_not_retried(env, monkeypatch):
    """M7：to_essence 体积超限是静态条件，必须一次终态失败。

    修复前抛普通 ValueError：队列把非 OneBotApiError 一律视为可重试，同一份
    32MiB+ 文档会被重新下载并按 2/4/8s 退避失败 4 次。
    """
    tmp_path, store, api, queue, ingest = env
    import core.application.ingest.fetch as fetch_mod

    downloads = {"n": 0}

    async def _counting_download(self, url, dest):
        downloads["n"] += 1
        dest.write_bytes(b"x" * 64)
        return 64

    monkeypatch.setattr(CloudIngestService, "_download", _counting_download)
    monkeypatch.setattr(fetch_mod, "_ESSENCE_TEXT_MAX_BYTES", 16)
    tid = await ingest.submit_fetch("g1", "https://example.com/big.txt", to_essence=True)
    r = await _wait_terminal(queue, tid, timeout=20)
    assert r["state"] == "failed"
    assert "exceeds" in r["error"]
    assert downloads["n"] == 1, f"静态条件被重试重下 {downloads['n']} 次"


# ---------- 低危：essence handler 的 pause_check ----------


@pytest.mark.asyncio
async def test_essence_save_loop_honors_pause(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    calls = {"n": 0}

    async def _spy(op):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OpPausedError()

    monkeypatch.setattr(queue, "pause_check", _spy)
    op = SimpleNamespace(
        task_id="t1",
        kind="essence_save",
        target="g1",
        cancel=False,
        pause=False,
        payload={
            "title": "长文归档",
            "text": "\n".join("字" * 500 for _ in range(4)),
        },
    )
    with pytest.raises(OpPausedError):
        await ingest._do_essence_save(op)
    assert calls["n"] == 2
    assert len(api.sent_messages) == 1, "第 2 片发送前就被打断"


@pytest.mark.asyncio
async def test_essence_delete_loop_honors_pause(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    calls = {"n": 0}

    async def _spy(op):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OpPausedError()

    monkeypatch.setattr(queue, "pause_check", _spy)
    op = SimpleNamespace(
        task_id="t1",
        kind="essence_delete",
        target="g1",
        cancel=False,
        pause=False,
        payload={"id": 1, "parts": [{"message_id": "1"}, {"message_id": "2"}]},
    )
    with pytest.raises(OpPausedError):
        await ingest._do_essence_delete(op)
    assert len(api.essence_deleted) == 1


# ---------- M8 ----------


@pytest.mark.asyncio
async def test_image_album_terminal_refusal_discards_staged_file(env):
    """M8：目标群无相册且协议端无 create_group_album 时必须一并清理暂存文件。

    该拒绝是 UNSUPPORTED（不可重试），但 _album_id 在 _require_album_upload 的
    try 之外抛出 → _discard_staged 不执行，tmp 里留下垃圾。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": []}

    async def _no_create(group_id, album_name, album_desc=""):
        api.calls.append("create_group_album")
        raise OneBotApiError(
            OneBotErrorKind.UNSUPPORTED, "create_group_album", "api not found"
        )

    api.create_group_album = _no_create
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "缺失相册")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert "手动创建" in r["error"]
    assert api.album_uploads == []
    assert not src.exists(), "终态拒绝必须一并清掉暂存文件"


@pytest.mark.asyncio
async def test_video_album_terminal_refusal_discards_staged_file(env, monkeypatch):
    """M8：视频相册分支的同类终态拒绝也必须清理暂存（否则留下 GB 级文件）。"""
    tmp_path, store, api, queue, ingest = env
    monkeypatch.setattr(CloudIngestService, "album_accepts_video", True)
    api.albums = {"g1": []}

    async def _no_create(group_id, album_name, album_desc=""):
        api.calls.append("create_group_album")
        raise OneBotApiError(
            OneBotErrorKind.UNSUPPORTED, "create_group_album", "api not found"
        )

    api.create_group_album = _no_create
    src = tmp_path / "v.mp4"
    src.write_bytes(b"fakemp4" * 10)
    tid = await ingest.submit_video_album("g1", src.as_posix(), "v.mp4", "缺失相册")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert api.album_uploads == []
    assert not src.exists(), "终态拒绝必须一并清掉暂存文件（video 源可能是 GB 级）"


# ---------- M2 ----------


def _replay_op(tmp_path, kind: str, name: str, staged: str):
    """构造一个「暂停 → 恢复」重放 op：replayed=True 但 retries 仍为 0。"""
    path = tmp_path / staged
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return SimpleNamespace(
        task_id="t1",
        kind=kind,
        target="g1",
        cancel=False,
        pause=False,
        retries=0,
        replayed=True,
        payload={"path": path.as_posix(), "name": name, "album_name": "测试相册"},
    )


@pytest.mark.asyncio
async def test_image_album_replay_guard_uses_replayed_not_retries(env):
    """M2：暂停 → 恢复也会重跑 handler 而 retries 仍为 0，判重必须用重放判据。"""
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {"g1:a1": [{"type": "image", "desc": "pic.png"}]}
    await ingest._do_image_album(_replay_op(tmp_path, "image_album", "pic.png", "pic.png"))
    assert api.album_uploads == [], "判重失效：同一张图被二次入册"


@pytest.mark.asyncio
async def test_video_album_replay_guard_uses_replayed_not_retries(env, monkeypatch):
    """M2：视频相册两条路径（直传 / 分片）的判重同样必须用 op.replayed。"""
    tmp_path, store, api, queue, ingest = env
    monkeypatch.setattr(CloudIngestService, "album_accepts_video", True)
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}

    # 直传路径（时长 < video_segment_seconds）：条目无 desc，走 name 回落
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(10))
    api.album_media = {"g1:a1": [{"name": "v.mp4"}]}
    await ingest._do_video_album(_replay_op(tmp_path, "video_album", "v.mp4", "v.mp4"))
    assert api.album_uploads == [], "直传路径判重失效"

    # 分片路径（时长 >= video_segment_seconds）：段名是确定性的
    import core.application.composition.splitter as splitter_mod

    async def _fake_split(src, out_dir, stem, max_sec):
        segs = []
        for i in range(1, 3):
            seg = out_dir / f"{stem}_seg{i:03d}.mp4"
            seg.write_bytes(b"SEG" * 10)
            segs.append(seg)
        return sorted(segs)

    monkeypatch.setattr(splitter_mod, "split_video", _fake_split)
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(1300))
    api.album_media = {
        "g1:a1": [{"file_name": "v 第1-2段.mp4"}, {"file_name": "v 第2-2段.mp4"}]
    }
    await ingest._do_video_album(_replay_op(tmp_path, "video_album", "v.mp4", "v2.mp4"))
    assert api.album_uploads == [], "分片路径判重失效"


# ---------- 低危：兜底常量统一为 QQ 硬限 ----------


def test_ingest_fallback_constants_match_qq_limits(tmp_path):
    """兜底常量必须钉在 QQ 硬限上，且 service.py 的 fallback 真的生效。

    旧断言是 CONST == 4000 == DEFAULTS[...] 的自比自（两边同源，一起改成 3000
    也通过），而且只比对常量、从不执行 fallback 逻辑。这里改为：常量对独立字面量
    （QQ 硬限）单向断言 + 真正构造 CloudIngestService 验证缺键时的实际取值。
    """
    from core.application.ingest.service import ESSENCE_CHUNK_FALLBACK_CHARS
    from core.application.ingest.video import VIDEO_SEGMENT_MAX_SECONDS
    from core.config.defaults import DEFAULTS

    # QQ 硬限是唯一事实来源：常量与 DEFAULTS 都必须等于它（不是互相比对）
    assert ESSENCE_CHUNK_FALLBACK_CHARS == 4000
    assert DEFAULTS["essence_chunk_size"] == 4000
    assert VIDEO_SEGMENT_MAX_SECONDS == 599
    assert DEFAULTS["video_segment_seconds"] == 599

    # 真实行为：配置缺键 → 落到兜底值；显式配置 → 覆盖兜底值
    def _svc(name, config):
        return CloudIngestService(
            api=object(),
            store=object(),
            queue=object(),
            sync=object(),
            tmp_dir=tmp_path / name,
            config=config,
        )

    fallback = _svc("t1", {})
    assert fallback.essence_chunk_chars == 4000
    assert fallback.video_segment_seconds == 599
    configured = _svc("t2", {"essence_chunk_size": 3000, "video_segment_seconds": 300})
    assert configured.essence_chunk_chars == 3000
    assert configured.video_segment_seconds == 300


def test_service_fallback_used_when_config_key_missing(tmp_path):
    """PluginConfig.get 是严格 dict.get（不回落 DEFAULTS）：空配置必须用 4000/599。"""
    svc = CloudIngestService(
        api=object(),
        store=object(),
        queue=object(),
        sync=object(),
        tmp_dir=tmp_path / "t",
        config={},
    )
    assert svc.essence_chunk_chars == 4000
    assert svc.video_segment_seconds == 599
