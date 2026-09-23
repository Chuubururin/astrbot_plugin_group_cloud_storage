"""CloudIngestService 测试（v1.2）：精华拆分存储 / HTTP-SFTP 外部导入 / 长视频分段。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from core.domain.enums import CapabilityState, OneBotApiError, OneBotErrorKind  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.ingest import essence  # noqa: E402
from core.application.composition.splitter import effective_chunk_limit, split_text  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402
from tests.contract.helpers import drain_op  # noqa: E402


@pytest.fixture
async def env(tmp_path, monkeypatch, request):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ingest: CloudIngestService | None = None
    # 标记 no_backoff 的用例不关心"等多久"，只关心"重试了几次/终态是什么"。
    # 退避与轮询间隔归零都不改变重试次数，也不改变 op.replayed 的置位。
    if request.node.get_closest_marker("no_backoff"):
        backoff_base = 0.0
        monkeypatch.setattr(essence, "CONFIRM_RETRY_INTERVAL", 0.0)
        monkeypatch.setattr(essence, "REBUILD_RETRY_INTERVAL", 0.0)
    else:
        backoff_base = 2.0
    queue = OpQueue(lambda op: ingest.handle(op),
                    backoff_base=backoff_base)
    await queue.start()
    ingest = CloudIngestService(
        api, store, queue, sync, tmp_dir=tmp_path / "tmp",
        config={"essence_chunk_size": 1000, "video_segment_seconds": 600,
                "fetch_max_bytes": 10 * 1024 * 1024, "fetch_timeout_sec": 10},
    )
    # 拉取下载字节走假实现（不真实联网）
    monkeypatch.setattr(CloudIngestService, "_download",
                        _fake_download)
    yield tmp_path, store, api, queue, ingest
    await queue.shutdown()
    await store.close()


async def _fake_download(self, url: str, dest: Path) -> int:
    data = b"HELLO-FROM-" + url.encode() * 64
    dest.write_bytes(data)
    return len(data)



# ---------- 纯函数：拆分 ----------

def test_split_text_limits_and_boundaries():
    text = "短文本"
    assert split_text(text, 10) == [text]
    long = "\n".join(f"第{i}行" for i in range(100))  # 每行 4 字
    chunks = split_text(long, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks) == long  # 行边界切分无损
    hard = "x" * 2500
    chunks = split_text(hard, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert len(chunks) == 3  # 2500 → 1000/1000/500


def test_split_text_sentence_boundary():
    text = ("。" * 999) + "！" + ("y" * 500)
    chunks = split_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0].endswith(("。", "！"))  # 句读边界优先


# ---------- 精华拆分存储 + 重建 ----------

@pytest.mark.asyncio
async def test_essence_save_split_and_rebuild(env):
    tmp_path, store, api, queue, ingest = env
    text = "\n".join(f"【第{i}段】" + "字" * 100 for i in range(30))  # ~3300 字
    tid = await ingest.submit_essence_save("g1", "长文归档", text)
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    n = len(api.sent_messages)
    assert n == len(api.essence_set) and n >= 4
    assert all(len(m["text"]) <= 1000 + 40 for m in api.sent_messages)
    # 每条消息带分片标记（2026-09-05 起标记在片尾：精华预览不被标记遮挡）
    for i, m in enumerate(api.sent_messages, 1):
        assert m["text"].endswith(f"[云盘|长文归档|{i}/{n}]")
    # 单逻辑资源索引
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert page.total == 1
    assert page.items[0].name == "长文归档"
    meta = page.items[0].meta or {}
    assert meta.get("kind") == "text_split" and len(meta.get("parts") or []) == n
    # 云端精华列表模拟 + 全文重建
    api.essences = {"g1": [
        {"message_id": m["message_id"],
         "content": [{"type": "text", "data": {"text": m["text"]}}],
         "sender_id": "10001"}
        for m in api.sent_messages
    ]}
    full, missing = await ingest.essence_full_text("g1", page.items[0].id)
    assert missing == []
    assert full == text


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_essence_rebuild_missing_part(env):
    tmp_path, store, api, queue, ingest = env
    tid = await ingest.submit_essence_save(
        "g1", "部分丢失",
        "\n".join("a" * 500 for _ in range(3)) + "\n" + "b" * 600 + "\n" + "c" * 600
    )
    await drain_op(queue, tid)
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    row_id = page.items[0].id
    # v2.4：保存即冗余分片文本 → 本地缓存快路径，云端不可用也完整重建
    full0, missing0 = await ingest.essence_full_text("g1", row_id)
    assert missing0 == []
    assert "a" * 500 in full0 and "c" * 600 in full0
    # 模拟旧数据（无本地分片文本）→ 云端部分缺失路径
    from core.domain.enums import ResourceType as _RT
    from core.domain.resource import Resource as _Res
    row = page.items[0]
    meta = row.meta or {}
    stripped = _Res(
        group_id=row.group_id, type=_RT.ESSENCE, name=row.name,
        source_ref=row.source_ref, size=row.size,
        meta={"kind": "text_split",
              "parts": [{"seq": p["seq"], "message_id": p.get("message_id"),
                         "chars": p.get("chars")}
                        for p in meta.get("parts", [])],
              "summary": meta.get("summary")},
    )
    await store.upsert_resources([stripped])
    page2 = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    row_id2 = page2.items[0].id
    # 云端仅存第 1 段
    m0 = api.sent_messages[0]
    api.essences = {"g1": [
        {"message_id": m0["message_id"],
         "content": [{"type": "text", "data": {"text": m0["text"]}}]}
    ]}
    full, missing = await ingest.essence_full_text("g1", row_id2)
    assert missing == [2, 3, 4, 5]
    # 片尾标记格式：正文 = 消息文本去掉末尾标记
    assert full == m0["text"][: m0["text"].rfind("\n[云盘|")]


# ---------- HTTP/SFTP 外部导入 ----------

@pytest.mark.asyncio
async def test_fetch_http_to_file(env):
    tmp_path, store, api, queue, ingest = env
    tid = await ingest.submit_fetch("g1", "https://example.com/a.zip", name="a.zip")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("upload_group_file:g1:a.zip") for c in api.calls)


@pytest.mark.asyncio
async def test_fetch_ftp_rejected(env):
    tmp_path, store, api, queue, ingest = env
    with pytest.raises(ValueError):
        await ingest.submit_fetch(
            "g1", "ftp://user:pw@127.0.0.1:2121/pub/b.zip", name="b.zip"
        )


@pytest.mark.asyncio
async def test_fetch_sftp_accepted(env):
    """ingress 入站协议白名单：sftp:// 属于合法拉取来源（文档契约 http/https/sftp/smb）。"""
    tmp_path, store, api, queue, ingest = env
    tid = await ingest.submit_fetch(
        "g1", "sftp://user:pw@example.com/pub/c.zip", name="c.zip"
    )
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any("upload_group_file:g1:c.zip" in c for c in api.calls)


@pytest.mark.asyncio
async def test_fetch_image_to_album(env):
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "我的相册"}]}
    api.essences = {"g1": []}
    tid = await ingest.submit_fetch(
        "g1", "https://example.com/pic.jpg", name="pic.jpg",
        to_album=True, album_name="我的相册",
    )
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert len(api.album_uploads) == 1
    up = api.album_uploads[0]
    assert up["group_id"] == "g1" and up["album_id"] == "a1"
    assert up["album_name"] == "我的相册"
    assert any(c.startswith("upload_image_to_qun_album") for c in api.calls)


@pytest.mark.asyncio
async def test_fetch_image_to_album_autocreate(env):
    """目标相册不存在时自动经协议端创建（2026-09-05 修复：此前直接报错断链）。"""
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": []}
    api.essences = {"g1": []}
    tid = await ingest.submit_fetch(
        "g1", "https://example.com/pic.jpg", name="pic.jpg",
        to_album=True, album_name="AstrBot云盘",
    )
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("create_group_album:g1:AstrBot云盘") for c in api.calls)
    assert len(api.album_uploads) == 1


@pytest.mark.asyncio
async def test_fetch_image_to_album_stale_declared_name(env):
    """同名 declared 暂存残留时仍以上报名为准（2026-09-12 真机坏链：
    旧"精华_N.png"残留让改名静默跳过，QQ 相册显示 fetch_xxx.tmp）。"""
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "我的相册"}]}
    api.essences = {"g1": []}
    # 预置同名残留（上一次运行遗留）
    (ingest.tmp_dir / "精华_9.png").write_bytes(b"stale")
    tid = await ingest.submit_fetch(
        "g1", "https://example.com/pic.png", name="精华_9.png",
        to_album=True, album_name="我的相册",
    )
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert len(api.album_uploads) == 1
    assert api.album_uploads[0]["file"].endswith("精华_9.png")


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_fetch_image_to_album_create_unsupported(env):
    """协议端无创建相册接口（NapCat）时报出可操作的中文指引。"""
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": []}
    api.essences = {"g1": []}

    async def _no_create(group_id, album_name, album_desc=""):
        raise RuntimeError("api not found: create_group_album")

    api.create_group_album = _no_create
    tid = await ingest.submit_fetch(
        "g1", "https://example.com/pic.jpg", name="pic.jpg",
        to_album=True, album_name="AstrBot云盘",
    )
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "failed"
    assert "手动创建" in r["error"]


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_schemes(env):
    """ingress 协议白名单（文档契约 http/https/sftp/smb）：ftp 明文协议一律拒绝。"""
    tmp_path, store, api, queue, ingest = env
    with pytest.raises(ValueError):
        await ingest.submit_fetch("g1", "ftp://host/x")
    with pytest.raises(ValueError):
        await ingest.submit_fetch("g1", "https://h/x.txt", name="x.txt",
                                  to_album=True)


# ---------- 长视频拆分存储 ----------

@pytest.mark.asyncio
async def test_video_direct_when_short(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(CloudIngestService, "_probe_duration",
                        _fixed_duration(120))
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "short.mp4")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert any(c.startswith("upload_group_file:g1:short.mp4") for c in api.calls)
    # 直传路径：不产生分片父资源
    vpage = await store.query_resources(ResourceQuery(group_id="g1"))
    assert not any(((it.meta or {}).get("kind") == "video") for it in vpage.items)


@pytest.mark.asyncio
async def test_video_boundary_599_splits(env, monkeypatch):
    """契约：≥599s 切为 <599s 段；恰为阈值（599s）的视频必须走分片。

    fixture 的分段阈值为 600，这里显式配置 599 验证「等于阈值即切」的边界。
    """
    tmp_path, store, api, queue, ingest = env
    ingest.video_segment_seconds = 599
    src = tmp_path / "edge.mp4"
    src.write_bytes(b"fakemp4" * 10)

    async def _fake_split(srcp, out_dir, stem, max_sec):
        seg = out_dir / f"{stem}_seg001.mp4"
        seg.write_bytes(b"SEG" * 10)
        return [seg]

    import core.application.ingest.video as video_mod

    monkeypatch.setattr(CloudIngestService, "_probe_duration",
                        _fixed_duration(599))
    monkeypatch.setattr(video_mod, "split_video", _fake_split)
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "edge.mp4")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    parts = [c for c in api.calls if c.startswith("upload_group_file:g1:edge.part")]
    assert len(parts) == 1


def _fixed_duration(sec):
    async def _probe(self, path):
        return sec
    return _probe


@pytest.mark.asyncio
async def test_video_split_upload(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "long.mp4"
    src.write_bytes(b"fakemp4" * 100)
    monkeypatch.setattr(CloudIngestService, "_probe_duration",
                        _fixed_duration(1300))

    async def _fake_split(srcp, out_dir, stem, max_sec):
        segs = []
        for i in range(1, 4):
            seg = out_dir / f"{stem}_seg{i:03d}.mp4"
            seg.write_bytes(f"SEG{i}".encode() * 100)
            segs.append(seg)
        return sorted(segs)

    # split_video 已统一到 composition.splitter（video.py 直接导入使用）
    import core.application.ingest.video as video_mod

    monkeypatch.setattr(video_mod, "split_video", _fake_split)
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "long.mp4")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    parts = [c for c in api.calls if c.startswith("upload_group_file:g1:long.part")]
    assert len(parts) == 3
    page = await store.query_resources(ResourceQuery(group_id="g1", type="file"))
    parents = [it for it in page.items if (it.meta or {}).get("kind") == "video"]
    assert len(parents) == 1
    assert (parents[0].meta or {}).get("volumes") is True
    assert (parents[0].meta or {}).get("total_seconds") == 1300
    vols = await store.list_volumes(parents[0].resource_id)
    assert len(vols) == 3
    assert all(v.status == "uploaded" and v.sha256 for v in vols)


# ---------- 视频分片重组下载（file_ops） ----------

@pytest.mark.asyncio
async def test_video_recon_concat(env, monkeypatch):
    tmp_path, store, api, queue, ingest = env
    ops = FileOpsService(api, store, queue, ingest.sync,
                         tmp_dir=tmp_path / "tmp2")
    from core.domain.resource import Resource
    from core.domain.enums import ResourceType as RT
    from core.domain.sync import VolumeInfo

    await store.upsert_resources([Resource(
        group_id="g1", type=RT.FILE, name="movie.mp4",
        source_ref="vidgroup:x", size=300,
        meta={"volumes": True, "kind": "video"},
    )])
    import hashlib as _h2

    await store.insert_volumes([
        VolumeInfo(parent_resource_id="g1:file:vidgroup:x", seq=1,
                   part_name="movie.part01.mp4", size=4,
                   sha256=_h2.sha256(b"AAAA").hexdigest(),
                   status="uploaded", source_ref="f1", busid=1),
        VolumeInfo(parent_resource_id="g1:file:vidgroup:x", seq=2,
                   part_name="movie.part02.mp4", size=4,
                   sha256=_h2.sha256(b"BBBB").hexdigest(),
                   status="uploaded", source_ref="f2", busid=1),
    ])

    detail = await store.get_resource_detail("g1", 1)
    assert detail and detail["resource_id"] == "g1:file:vidgroup:x"

    seg_bytes = [b"AAAA", b"BBBB"]
    async def _fetch(self, url, dest):
        # 重组走流式落盘下载（_download_to_file）：分段直写磁盘再哈希
        data = seg_bytes.pop(0)
        dest.write_bytes(data)
        return len(data)
    monkeypatch.setattr(FileOpsService, "_download_to_file", _fetch)

    captured = {}

    def fake_run(cmd, **kw):
        list_file = Path(cmd[cmd.index("-i") + 1])
        out = Path(cmd[-1])
        data = b""
        for line in list_file.read_text().splitlines():
            p = line.split("'")[1]
            data += Path(p).read_bytes()
        out.write_bytes(data)
        captured["out"] = out
        return type("R", (), {"returncode": 0, "stderr": ""})()

    # file_ops 内部 import subprocess —— 直接补丁模块属性
    monkeypatch.setattr("subprocess.run", fake_run)
    out, name = await ops.download_info("g1", 1)
    assert name == "movie.mp4"
    assert Path(out).read_bytes() == b"AAAABBBB"


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_essence_save_retries_on_dropped_set(env):
    """v1.2：QQ 偶发丢设精华 → 逐段回读验证 + 仅重设精华（不重发消息），最终全部确认。"""
    tmp_path, store, api, queue, ingest = env
    api.drop_first_set = 1
    text = "A" * 1200 + "\n" + "B" * 800
    tid = await ingest.submit_essence_save("g1", "丢设重试", text)
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "ok"
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert page.total == 1
    meta = page.items[0].meta or {}
    assert len(meta.get("parts") or []) == 3
    full, missing = await ingest.essence_full_text("g1", page.items[0].id)
    assert missing == []
    # 硬切分会在切点引入换行：重建结果 = 分片拼接（切分函数幂等口径）
    limit = effective_chunk_limit("丢设重试", 3, ingest.essence_chunk_chars)
    assert full == "\n".join(split_text(text, limit))
    # 2026-09-11：确认失败只重设精华、不重发消息（真机上 NapCat 静默吞掉
    # 服务端拒绝时，重发只会刷屏而精华永远不落地）→ 发送数恒等于分片数
    assert len(api.sent_messages) == 3


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_essence_save_unconfirmable_fails_without_resend(env):
    """设精始终不落地（权限不足被 NapCat 静默吞掉）→ 每分片只发一条消息，
    任务失败且不触发队列级重试（LOCAL_ERROR），避免刷屏。"""
    tmp_path, store, api, queue, ingest = env
    real_set = api.set_essence_msg

    async def silent_fail(message_id: str) -> None:
        await real_set(message_id)
        api.essence_set = [
            m for m in api.essence_set if m != str(message_id)
        ]
        api.essences = {
            g: [e for e in items if str(e.get("message_id")) != str(message_id)]
            for g, items in api.essences.items()
        }

    api.set_essence_msg = silent_fail
    tid = await ingest.submit_essence_save("g1", "设精失败", "x" * 300)
    r = await drain_op(queue, tid, timeout=30)
    assert r["state"] == "failed"
    assert "not confirmed" in str(r.get("error") or "")
    assert len(api.sent_messages) == 1  # 无队列级重跑 → 不重发


@pytest.mark.asyncio
async def test_essence_delete_parts(env):
    """v1.2：精华删除逐分片移出 + 资源软删。"""
    tmp_path, store, api, queue, ingest = env
    tid = await ingest.submit_essence_save("g1", "待删除", "x" * 100 + "\n" + "y" * 100)
    await drain_op(queue, tid)
    page = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    row = next((i for i in page.items if i.name == "待删除"), None)
    assert row is not None
    parts = (row.meta or {}).get("parts") or []
    tid2 = await ingest.submit_essence_delete("g1", row.id)
    r = await drain_op(queue, tid2)
    assert r["state"] == "ok"
    assert sorted(api.essence_deleted) == sorted(
        p["message_id"] for p in parts
    )
    page2 = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    assert all(i.name != "待删除" for i in page2.items)
    # 拒绝非精华资源
    with pytest.raises(ValueError):
        await ingest.submit_essence_delete("g1", 999999)


# ---------- v2.4：预览离线化（本地缓存快路径 + 云端超时） ----------

@pytest.mark.asyncio
async def test_essence_full_text_local_cache_fast_path(env, monkeypatch):
    """保存时分片已冗余本地文本 → 重建全文不发任何云端调用。"""
    _, store, api, _, ingest = env
    from core.domain.enums import ResourceType
    from core.domain.resource import Resource

    await store.upsert_resources([Resource(
        group_id="g1", type=ResourceType.ESSENCE, name="缓存文",
        source_ref="text:local1", size=6,
        meta={"kind": "text_split", "parts": [
            {"seq": 1, "message_id": "m1", "chars": 3, "text": "第一段"},
            {"seq": 2, "message_id": "m2", "chars": 3, "text": "第二段"},
        ], "summary": "第一段"},
    )])
    rows = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    rid = rows.items[0].id
    calls_before = len(api.calls)
    text, missing = await ingest.essence_full_text("g1", rid)
    assert text == "第一段\n第二段"
    assert missing == []
    assert len(api.calls) == calls_before  # 零云端调用（离线秒开）


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_essence_full_text_cloud_timeout_raises(env, monkeypatch):
    """云端精华列表挂起 → 超时抛出清晰错误（不无限等待）。"""
    import core.application.ingest.essence as ci
    _, store, api, _, ingest = env
    from core.domain.enums import ResourceType
    from core.domain.resource import Resource

    monkeypatch.setattr(ci, "CLOUD_CALL_TIMEOUT", 0.05)

    async def slow_list(group_id):
        await asyncio.sleep(5.0)
        return []

    api.get_essence_msg_list = slow_list
    await store.upsert_resources([Resource(
        group_id="g1", type=ResourceType.ESSENCE, name="云端文",
        source_ref="text:local2", size=4,
        meta={"kind": "text_split", "parts": [{"seq": 1, "chars": 4}],
              "summary": "云端"},
    )])
    rows = await store.query_resources(ResourceQuery(group_id="g1", type="essence"))
    rid = rows.items[0].id
    with pytest.raises(TimeoutError):
        await ingest.essence_full_text("g1", rid)


# ---------- v2.5：相册视频关键帧 GIF 预览 ----------

@pytest.mark.asyncio
async def test_video_preview_gif_generates_and_caches(env, tmp_path, monkeypatch):
    """视频预览：下载→抽帧→GIF→缓存；二次调用命中缓存（零重复下载）。"""
    import base64
    import shutil as _sh
    import subprocess as _sp

    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    _, store, api, _, ingest = env

    # 生成 3 秒测试视频
    src_video = tmp_path / "sample.mp4"
    proc = _sp.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(src_video)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-200:]

    # 云端媒体条目：video_url 为字符串化 spec 列表
    api.album_media = {"g1:aid1": [{
        "type": "video", "desc": "测试视频",
        "video": {
            "id": "v1", "video_time": 3000,
            # NapCat 实际返回 JSON 数组形态
            "video_url": [{"spec": 5, "url": {"url": "https://fake/v.mp4",
                                              "width": 320, "height": 240}}],
        },
    }]}

    # 下载步骤替换为本地拷贝（不真实联网）
    calls = {"n": 0}

    async def fake_download(self, url, cache_dir, cache_key):
        calls["n"] += 1
        import shutil as _s2
        from pathlib import Path as _P
        dest = _P(cache_dir) / f"{cache_key}.mp4"
        _s2.copyfile(src_video, dest)
        return dest

    monkeypatch.setattr(type(ingest), "_download_video", fake_download)

    out = await ingest.video_preview_gif("g1", "aid1", "测试视频")
    data = base64.b64decode(out["gif_base64"])
    assert data[:6] in (b"GIF89a", b"GIF87a")
    assert out["frames"] == 9 and out["duration_ms"] == 3000
    assert out["bytes"] > 0

    # 二次调用命中磁盘缓存：不重复下载、结果一致
    out2 = await ingest.video_preview_gif("g1", "aid1", "测试视频")
    assert calls["n"] == 1
    assert out2["gif_base64"] == out["gif_base64"]


@pytest.mark.asyncio
async def test_video_preview_missing_entry_raises(env):
    """视频条目不存在/无 URL → 清晰 ValueError（前端 toast 而非挂起）。"""
    _, store, api, _, ingest = env
    api.album_media = {"g1:aid1": [{"type": "video", "desc": "别的视频",
                                    "video": {}}]}
    with pytest.raises(ValueError):
        await ingest.video_preview_gif("g1", "aid1", "不存在的视频")
    with pytest.raises(ValueError):
        await ingest.video_preview_gif("g1", "aid1", "别的视频")  # 无 video_url


@pytest.mark.asyncio
async def test_video_album_splits_and_uploads(env, monkeypatch):
    """化整为零（v2.8）：媒体分片入群相册——ffmpeg 分段逐段上传 + 相册刷新。

    仅在协议端支持相册视频时可达（`album_accepts_video`，默认关闭，见下一条）。
    """
    import shutil as _sh
    import subprocess as _sp

    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    monkeypatch.setattr(CloudIngestService, "album_accepts_video", True)
    tmp_path, store, api, _, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    src = tmp_path / "v.mp4"
    proc = _sp.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=2:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    tid = await ingest.submit_video_album("g1", src.as_posix(), "v.mp4", "测试相册")
    await drain_op(ingest.queue, tid)
    assert len(api.album_uploads) >= 1
    assert api.album_uploads[0]["album_name"] == "测试相册"
    assert "get_qun_album_list" in " ".join(api.calls)


@pytest.mark.asyncio
async def test_video_album_refused_on_image_only_protocol(env):
    """默认协议端相册只收图片：视频任务一次终态拒绝，且不做任何 ffmpeg/相册操作。

    真机证据（2026-09-16）：NapCat `upload_image_to_qun_album` 对视频返回
    retcode=100「群相册上传仅支持 JPEG、PNG、GIF、WebP 或 BMP 图片」。
    拒绝必须发生在切割之前（否则长视频会被完整切分后才逐段被拒），且不可重试：
    按 ValueError 抛会被队列重试 3 次（2+4+8s = 14s），6s 的 drain 超时会直接失败。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    src = tmp_path / "v.mp4"
    src.write_bytes(b"fakemp4" * 10)  # 无需 ffmpeg：拒绝先于时长探测/切割
    tid = await ingest.submit_video_album("g1", src.as_posix(), "v.mp4", "测试相册")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert "仅支持图片" in r["error"]
    assert "入群文件" in r["error"]  # 可操作指引
    assert api.album_uploads == []
    assert not src.exists()  # 拒绝是终态：暂存文件一并清掉（否则 tmp 里留垃圾）
    assert not any(
        c.startswith(("get_qun_album_list", "create_group_album")) for c in api.calls
    )


@pytest.mark.asyncio
async def test_image_album_single_upload(env):
    """2026-09-01 N-06：单图导入群相册（upload_image_to_qun_album + 资源化刷新）。"""
    tmp_path, store, api, _, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "测试相册")
    await drain_op(ingest.queue, tid)
    assert len(api.album_uploads) >= 1
    assert api.album_uploads[0]["album_name"] == "测试相册"
    assert "get_qun_album_list" in " ".join(api.calls)
    assert not src.exists()  # 暂存已清理


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_image_album_replay_does_not_duplicate_media(env):
    """重试不得把同一张图二次入册（上传非幂等）。

    场景：服务端已收下媒体，但调用以超时收场（TIMEOUT 可重试）。旧实现重放整个
    任务会再传一次，相册里出现两张同名图。重放时必须先查相册媒体列表再决定。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {}
    uploads = {"n": 0}
    real_upload = api.upload_image_to_qun_album

    async def _flaky(group_id, album_id, album_name, file):
        uploads["n"] += 1
        await real_upload(group_id, album_id, album_name, file)
        # 服务端已收下：相册媒体列表出现该条目
        api.album_media.setdefault(f"{group_id}:{album_id}", []).append(
            {"type": "image", "desc": Path(file).name}
        )
        if uploads["n"] == 1:
            raise OneBotApiError(
                OneBotErrorKind.TIMEOUT, "upload_image_to_qun_album", "timeout"
            )

    api.upload_image_to_qun_album = _flaky
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "测试相册")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert uploads["n"] == 1  # 重放未再上传
    assert len(api.album_uploads) == 1
    assert any(c.startswith("get_group_album_media_list") for c in api.calls)


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_image_album_replay_dedup_falls_back_to_name_and_file_name(env):
    """去重键必须回落到 name / file_name：真实协议端的条目不一定给 desc。

    旧用例只覆盖 desc（{"desc": ...}）；媒体条目只有 name / file_name 时去重会
    静默失效，重放把同一张图再传一遍。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {}
    uploads = {"n": 0}
    real_upload = api.upload_image_to_qun_album

    async def _flaky(group_id, album_id, album_name, file):
        uploads["n"] += 1
        await real_upload(group_id, album_id, album_name, file)
        # 服务端已收下，但条目里没有 desc（真实协议端的常见形态）
        api.album_media.setdefault(f"{group_id}:{album_id}", []).append(
            {"type": "image", "name": Path(file).name}
        )
        if uploads["n"] == 1:
            raise OneBotApiError(
                OneBotErrorKind.TIMEOUT, "upload_image_to_qun_album", "timeout"
            )

    api.upload_image_to_qun_album = _flaky
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "测试相册")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert uploads["n"] == 1, "name 回落失效：重放把同一张图又传了一遍"

    # 三个回落分支都必须命中（desc / name / file_name），且不误判别的名字
    for entry in ({"desc": "封面.png"}, {"name": "封面.png"}, {"file_name": "封面.png"}):
        api.album_media = {"g1:a1": [entry]}
        assert await ingest._album_has_media("g1", "a1", "封面.png") is True, entry
    api.album_media = {"g1:a1": [{"desc": "别的.png"}]}
    assert await ingest._album_has_media("g1", "a1", "封面.png") is False


@pytest.mark.no_backoff
@pytest.mark.asyncio
async def test_image_album_replay_reuses_renamed_staged_file(env):
    """真机坏链（2026-09-16）：改名后的重放必须找到改名后的文件。

    首次尝试把 uuid 暂存名改成 declared 名再上传；上传以 REMOTE_ERROR 收场
    （可重试分类），此时 payload["path"] 已不存在。旧实现重放时找不到源文件：
    图片路径抛 ValueError（又被重试 3 次），视频路径掉进切割分支报出误导性的
    「ffmpeg split failed: No such file or directory」。改名后的路径必须写回 payload。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.album_media = {}
    attempts = {"n": 0}
    real_upload = api.upload_image_to_qun_album

    async def _flaky(group_id, album_id, album_name, file):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # 协议端拒绝且未落地：重放必须能重传（而非报「暂存文件已不存在」）
            raise OneBotApiError(
                OneBotErrorKind.REMOTE_ERROR, "upload_image_to_qun_album", "boom"
            )
        await real_upload(group_id, album_id, album_name, file)

    api.upload_image_to_qun_album = _flaky
    staged = tmp_path / "fetch_img_ab12cd.png"  # uuid 前缀暂存名
    staged.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album(
        "g1", staged.as_posix(), "封面.png", "测试相册"
    )
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert attempts["n"] == 2  # 重放确实重传了，而不是找不到文件
    assert api.album_uploads[0]["file"].endswith("封面.png")  # 仍以 declared 名入册
    assert not staged.exists()


@pytest.mark.asyncio
async def test_image_album_missing_staged_file_fails_once(env):
    """暂存文件缺失是本地静态条件：一次终态失败，不重试也不误报 ffmpeg。

    旧实现抛 ValueError（队列视为可重试）→ 重试 3 次共 14s 后仍报同一错误；
    视频路径更会掉进切割分支，报出误导性的「ffmpeg split failed」。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    missing = tmp_path / "gone.png"  # 从未创建
    tid = await ingest.submit_image_album(
        "g1", missing.as_posix(), "gone.png", "测试相册"
    )
    r = await drain_op(queue, tid, timeout=6)  # 重试版本会超时（2+4+8s）
    assert r["state"] == "failed"
    assert "暂存文件已不存在" in r["error"]
    assert api.album_uploads == []


# ---------- 相册上传的协议端能力探测（明示降级） ----------

@pytest.mark.asyncio
async def test_video_album_degrades_when_album_upload_unsupported(env):
    """`upload_image_to_qun_album` 不可用（非 NapCat/旧版协议端）时立即降级。

    docs/入库与组合存储.md「已知限制」要求相册视频上传经协议端能力探测、不可用时
    明示降级。两点必须同时成立：
    - 探测在 ffmpeg 切割之前（否则长视频会被完整切分后才逐段被拒）；
    - 失败不可重试（UNSUPPORTED 语义）。按 ValueError 抛出会被队列重试 3 次
      （2+4+8s = 14s），6s 的 drain 超时正是为此——重试版本会直接超时。
    """
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.capability = lambda action: CapabilityState.UNSUPPORTED
    src = tmp_path / "v.mp4"
    src.write_bytes(b"fakemp4" * 10)  # 无需 ffmpeg：探测先于时长/切割
    tid = await ingest.submit_video_album("g1", src.as_posix(), "v.mp4", "测试相册")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert "upload_image_to_qun_album" in r["error"]
    assert api.album_uploads == []  # 未提交注定失败的上传
    # 探测在最前：既未解析相册，也未尝试建相册
    assert not any(c.startswith(("get_qun_album_list", "create_group_album")) for c in api.calls)


@pytest.mark.asyncio
async def test_image_album_degrades_when_album_upload_unsupported(env):
    """图片路径同款降级（两条路径共用 upload_image_to_qun_album）。"""
    tmp_path, store, api, queue, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    api.capability = lambda action: CapabilityState.UNSUPPORTED
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "测试相册")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert "upload_image_to_qun_album" in r["error"]
    assert api.album_uploads == []


@pytest.mark.asyncio
async def test_album_create_unsupported_fails_once_not_retried(env):
    """协议端无创建相册接口（NapCat）时立即终态，且只尝试一次。

    「没有该接口」是静态条件：旧实现把它包成 ValueError，队列会重试 3 次
    （每次重新列一遍相册 + 再试一次创建）后才报错。改为 UNSUPPORTED 语义后
    应当只尝试一次并给出可操作指引。
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
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "AstrBot云盘")
    r = await drain_op(queue, tid, timeout=6)
    assert r["state"] == "failed"
    assert "手动创建" in r["error"]
    assert api.calls.count("create_group_album") == 1  # 非重试：只尝试一次
    assert api.album_uploads == []


@pytest.mark.asyncio
async def test_fetch_to_essence_url_doc(env):
    """2026-09-01 N-06：URL 文档读取 → 文本分段保存为精华消息（to_essence 通道）。"""
    tmp_path, store, api, _, ingest = env

    async def fake_download(url, dest):
        dest.write_text("这是从 URL 拉取的文档正文，超过 4000 字符需分片。" * 120, encoding="utf-8")
        return len("x" * 100)

    ingest.transfer = type("T", (), {"download_to": fake_download})()
    tid = await ingest.submit_fetch("g1", "https://example.com/doc.txt", name="doc.txt", to_essence=True)
    await drain_op(ingest.queue, tid)
    # 精华保存后资源目录有 kind=essence 行（name 同步）
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", type="essence", page_size=10
        )
    )
    assert any(it.name == "doc.txt" or it.name.startswith("doc") for it in page.items)


@pytest.mark.asyncio
async def test_fetch_to_essence_mutually_exclusive(env):
    """to_album 与 to_essence 互斥（N-06）。"""
    tmp_path, store, api, _, ingest = env
    with pytest.raises(ValueError):
        await ingest.submit_fetch(
            "g1", "https://example.com/a.png", to_album=True, to_essence=True
        )


# ---------- M9：短视频直传必须清理暂存源文件 ----------

@pytest.mark.asyncio
async def test_video_direct_cleans_staged_source(env, monkeypatch):
    """M9：直传是常态路径，返回前必须删掉暂存视频。

    修复前：直传分支 return 前不 unlink，暂存视频一直留到下次进程重启才被清扫。
    """
    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(120))
    tid = await ingest.submit_video_upload("g1", src.as_posix(), "short.mp4")
    r = await drain_op(queue, tid)
    assert r["state"] == "ok"
    assert not src.exists()


@pytest.mark.asyncio
async def test_video_direct_keeps_staged_source_on_retriable_failure(env, monkeypatch):
    """H1：直传抛可重试错误（TIMEOUT）时必须保留暂存源文件。

    修复前 `finally` 无条件 unlink：队列重放时 src.exists() 为假 → 命中「暂存文件
    已不存在」LOCAL_ERROR（不可重试）→ 3 次重试预算作废、用户视频丢失。分片分支
    特意保留源文件，直传分支却删掉，两个分支对重试的契约自相矛盾。

    这里是 handler 级断言（快速、确定性）；真实的队列 retries/backoff 重放路径见
    tests/unit/test_ingest_regressions.py::
    test_video_direct_retriable_failure_keeps_source_for_replay。
    """
    from types import SimpleNamespace

    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "short.mp4"
    src.write_bytes(b"fakemp4" * 10)
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(120))

    async def _boom(group_id, file_path, name="", folder_id=None, folder="",
                    upload_file=True):
        raise OneBotApiError(OneBotErrorKind.TIMEOUT, "upload_group_file", "boom")

    monkeypatch.setattr(api, "upload_group_file", _boom)
    op = SimpleNamespace(
        task_id="t1", kind="video_upload", target="g1", cancel=False, pause=False,
        payload={"path": src.as_posix(), "name": "short.mp4", "folder_id": ""},
    )
    with pytest.raises(OneBotApiError) as ei:
        await ingest._do_video_upload(op)
    assert ei.value.kind is OneBotErrorKind.TIMEOUT
    assert src.exists(), "可重试失败必须保留暂存源文件供重放"


# ---------- M13：分段上传重试必须跳过已上传分片 ----------

@pytest.mark.asyncio
async def test_video_retry_skips_already_uploaded_parts(env, monkeypatch):
    """M13：分片上传失败后重试，已上传分片不得再传一遍。

    修复前：上传动作从 seq=1 重跑，QQ 侧出现同名重复分片。
    """
    from types import SimpleNamespace

    tmp_path, store, api, queue, ingest = env
    src = tmp_path / "long.mp4"
    src.write_bytes(b"fakemp4" * 100)
    monkeypatch.setattr(CloudIngestService, "_probe_duration", _fixed_duration(1300))

    async def _fake_split(srcp, out_dir, stem, max_sec):
        segs = []
        for i in range(1, 4):
            seg = out_dir / f"{stem}_seg{i:03d}.mp4"
            seg.write_bytes(f"SEG{i}".encode() * 100)
            segs.append(seg)
        return sorted(segs)

    import core.application.ingest.video as video_mod

    monkeypatch.setattr(video_mod, "split_video", _fake_split)

    op = SimpleNamespace(
        task_id="t1", kind="video_upload", target="g1", cancel=False, pause=False,
        payload={"path": src.as_posix(), "name": "long.mp4", "folder_id": ""},
    )
    real_upload = api.upload_group_file
    seen = {"n": 0}

    async def _flaky(group_id, file_path, name="", folder_id=None, folder="",
                     upload_file=True):
        seen["n"] += 1
        if seen["n"] == 2:  # 第 2 片失败 -> 真实队列会带着同一 payload 重试
            raise OneBotApiError(OneBotErrorKind.LOCAL_ERROR, "upload_group_file", "boom")
        return await real_upload(group_id, file_path, name=name, folder_id=folder_id,
                                 folder=folder, upload_file=upload_file)

    monkeypatch.setattr(api, "upload_group_file", _flaky)
    with pytest.raises(OneBotApiError):
        await ingest._do_video_upload(op)
    parent_key = f"g1:file:{op.payload['parent_resource_id']}"
    assert [v.seq for v in await store.list_volumes(parent_key)] == [1]

    # 手工二次进入同一个 op / payload，模拟队列重放：parent_resource_id 被复用。
    # （这里的失败是 LOCAL_ERROR，不可重试，走不到队列重放；真实队列的
    #  retries/backoff 重派路径见 tests/unit/test_ingest_regressions.py
    #  ::test_video_split_retry_skips_uploaded_parts）
    monkeypatch.setattr(api, "upload_group_file", real_upload)
    api.calls.clear()
    await ingest._do_video_upload(op)

    parts = [c for c in api.calls if "long.part" in c]
    assert len(parts) == 2, parts  # 只补传 part02/part03
    assert all("part01" not in c for c in parts), parts
    assert [v.seq for v in await store.list_volumes(parent_key)] == [1, 2, 3]


# ---------- 低危：兜底常量 / URL 名字长度 / 大文档上限 / pause_check ----------

@pytest.mark.asyncio
async def test_ingest_fallback_limits_match_qq_hard_limits(tmp_path):
    """低危：配置缺键时的兜底必须与 defaults.py / _conf_schema.json 一致（4000/599）。

    PluginConfig.get 是严格 dict.get（不回落 DEFAULTS），所以缺键时 base 就取
    兜底值；之前 ingest 写 4500/600、album 写 599，三处不一致。
    """
    import core.application.ingest.video as video_mod
    from core.application.ingest.service import ESSENCE_CHUNK_FALLBACK_CHARS

    assert ESSENCE_CHUNK_FALLBACK_CHARS == 4000
    assert video_mod.VIDEO_SEGMENT_MAX_SECONDS == 599

    # 自建的 store 必须自己关：同文件其他用例都关，否则 SQLite 连接泄漏
    store = SqliteMetaStore(tmp_path / "m.db")
    await store.init()
    try:
        svc = CloudIngestService(
            FakeOneBotApi(tree={None: ([], [])}), store,
            OpQueue(lambda op: None),
            ResourceSyncService(FakeOneBotApi(tree={None: ([], [])}), store),
            tmp_dir=tmp_path / "t", config={},
        )
        assert svc.essence_chunk_chars == 4000
        assert svc.video_segment_seconds == 599
    finally:
        await store.close()


def test_fetch_name_clamped_to_downstream_contract():
    """低危：URL 路径段派生的名字必须满足 1..80 契约。

    修复前：长 URL 文件名会让整次导入在**下载完成后**才失败并重试 3 次。
    """
    from core.application.ingest.fetch import _clamp_name

    assert _clamp_name("a.txt") == "a.txt"
    out = _clamp_name("x" * 200 + ".mp4")
    assert 1 <= len(out) <= 80
    assert out.endswith(".mp4")  # 保留扩展名（媒体类型判定仍有效）
    assert len(_clamp_name("y" * 300)) == 80
    assert _clamp_name("") == "fetched"


@pytest.mark.asyncio
async def test_fetch_to_essence_rejects_oversized_document(env, monkeypatch):
    """低危：to_essence 不再把整个下载文件一次性读入内存（加大小上限）。"""
    from types import SimpleNamespace

    import core.application.ingest.fetch as fetch_mod

    tmp_path, store, api, _, ingest = env
    monkeypatch.setattr(fetch_mod, "_ESSENCE_TEXT_MAX_BYTES", 16)

    async def fake_download(url, dest):
        dest.write_bytes(b"x" * 64)
        return 64

    ingest.transfer = type("T", (), {"download_to": fake_download})()
    op = SimpleNamespace(
        task_id="t1", kind="fetch", target="g1", cancel=False, pause=False,
        payload={"url": "https://example.com/big.txt", "name": "big.txt",
                 "to_essence": True},
    )
    with pytest.raises(OneBotApiError, match="exceeds") as ei:
        await ingest._do_fetch(op)
    # 静态条件必须不可重试，否则整份大文档会被重下 4 次（2/4/8s 退避）
    assert ei.value.kind is OneBotErrorKind.LOCAL_ERROR


@pytest.mark.asyncio
async def test_essence_save_loop_honours_pause_check(env):
    """低危：长文本分段保存的循环里必须有 pause_check（否则无法中断）。"""
    from types import SimpleNamespace

    from core.application.queue import OpCancelError

    tmp_path, store, api, queue, ingest = env
    op = SimpleNamespace(
        task_id="t1", kind="essence_save", target="g1", cancel=True, pause=False,
        payload={"title": "标题", "text": "正文内容" * 2000},
    )
    with pytest.raises(OpCancelError):
        await ingest._do_essence_save(op)
    assert not api.sent_messages  # 一个分段都不应发出


@pytest.mark.asyncio
async def test_essence_delete_loop_honours_pause_check(env):
    """低危：多分片删除的循环里必须有 pause_check。"""
    from types import SimpleNamespace

    from core.application.queue import OpCancelError

    tmp_path, store, api, queue, ingest = env
    op = SimpleNamespace(
        task_id="t1", kind="essence_delete", target="g1", cancel=True, pause=False,
        payload={"id": 1, "parts": [{"seq": 1, "message_id": "m1"}]},
    )
    with pytest.raises(OpCancelError):
        await ingest._do_essence_delete(op)
    assert not api.essence_deleted
