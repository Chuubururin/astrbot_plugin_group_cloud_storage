"""CloudIngestService 测试（v1.2）：精华拆分存储 / HTTP-SFTP 外部导入 / 长视频分段。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.sync import ResourceQuery  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402
from core.application.composition.splitter import effective_chunk_limit, split_text  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402
from tests.contract.helpers import drain_op  # noqa: E402


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ingest: CloudIngestService | None = None
    queue = OpQueue(lambda op: ingest.handle(op), interval=0.0)
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
    import json

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
    import hashlib as _hl

    detail = await store.get_resource_detail("g1", 1)
    assert detail and detail["resource_id"] == "g1:file:vidgroup:x"

    seg_bytes = [b"AAAA", b"BBBB"]
    async def _fetch(self, url):
        return seg_bytes.pop(0)
    monkeypatch.setattr(FileOpsService, "_fetch_bytes", _fetch)

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


@pytest.mark.asyncio
async def test_essence_save_retries_on_dropped_set(env):
    """v1.2：QQ 偶发丢设精华 → 逐段回读验证 + 重发重设，最终全部确认。"""
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
    assert len(api.sent_messages) >= 3  # 至少一段重发过


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
async def test_video_album_splits_and_uploads(env):
    """化整为零（v2.8）：媒体分片入群相册——ffmpeg 分段逐段上传 + 相册刷新。"""
    import shutil as _sh
    import subprocess as _sp

    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
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
    await drain_op(queue := ingest.queue, tid)
    assert len(api.album_uploads) >= 1
    assert api.album_uploads[0]["album_name"] == "测试相册"
    assert "get_qun_album_list" in " ".join(api.calls)


@pytest.mark.asyncio
async def test_image_album_single_upload(env):
    """2026-09-01 N-06：单图导入群相册（upload_image_to_qun_album + 资源化刷新）。"""
    tmp_path, store, api, _, ingest = env
    api.albums = {"g1": [{"album_id": "a1", "name": "测试相册"}]}
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tid = await ingest.submit_image_album("g1", src.as_posix(), "pic.png", "测试相册")
    await drain_op(queue := ingest.queue, tid)
    assert len(api.album_uploads) >= 1
    assert api.album_uploads[0]["album_name"] == "测试相册"
    assert "get_qun_album_list" in " ".join(api.calls)
    assert not src.exists()  # 暂存已清理


@pytest.mark.asyncio
async def test_fetch_to_essence_url_doc(env):
    """2026-09-01 N-06：URL 文档读取 → 文本分段保存为精华消息（to_essence 通道）。"""
    tmp_path, store, api, _, ingest = env

    async def fake_download(url, dest):
        dest.write_text("这是从 URL 拉取的文档正文，超过 4000 字符需分片。" * 120, encoding="utf-8")
        return len("x" * 100)

    ingest.transfer = type("T", (), {"download_to": fake_download})()
    tid = await ingest.submit_fetch("g1", "https://example.com/doc.txt", name="doc.txt", to_essence=True)
    await drain_op(queue := ingest.queue, tid)
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
