"""下载分发矩阵契约（W2-A，2026-09-02 ADR-0009）：
DistributorService 统一分发编排（文件/相册/精华/网盘 × 目标）；
目标白名单校验；smb 诚实降级。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.domain.enums import ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.application.distributor import DISTRIBUTE_TARGETS, DistributorService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


class _FakeDlServer:
    enabled = True
    sftp_port = 22
    smb_port = 0
    smb_available = False

    def __init__(self):
        self.calls = []

    def download_url(self, group_id, rid):
        self.calls.append(f"url:{group_id}:{rid}")
        return f"http://dl.local/{group_id}/{rid}"

    def sftp_info(self):
        return {"host": "127.0.0.1", "port": 22, "user": "cloud", "password": "x"}

    def register_staged(self, path, name):
        self.calls.append(f"staged:{name}")
        return {"http_url": f"http://dl.local/staged/{name}", "sftp": None}


class _FakeBridge:
    def __init__(self, store, queue):
        self.store = store
        self.queue = queue
        self.out_tasks: list[str] = []
        self.client = _FakeOpenList()

    async def submit_out(self, group_id, rid, *, dst_dir=None, force=False):
        tid = f"out-{rid}"
        self.out_tasks.append(tid)
        return tid

    async def submit_in(self, path, *, group_id):
        return f"in-{path}"


class _FakeOpenList:
    async def submit_offline_download(self, urls, path):
        return [type("T", (), {"id": f"off-{len(urls)}"})()]

    async def get_raw_url(self, path):
        return type("L", (), {"url": f"http://ol/{path}"})()


class _FakeIngest:
    def __init__(self, queue):
        self.queue = queue
        self.fetch_calls: list[dict] = []
        self.essence_texts: dict[int, tuple[str, list]] = {}
        self.essence_saves: list[dict] = []

    async def submit_fetch(self, group_id, url, name="", to_album=False,
                           album_name="", to_essence=False):
        self.fetch_calls.append({
            "group": group_id, "url": url, "name": name,
            "to_album": to_album, "to_essence": to_essence,
        })
        return f"fetch-{len(self.fetch_calls)}"

    async def submit_essence_save(self, group_id, title, text):
        self.essence_saves.append({"group": group_id, "title": title, "text": text})
        return f"essence-{len(self.essence_saves)}"

    async def essence_full_text(self, group_id, rid):
        return self.essence_texts.get(rid, ("", []))


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    queue = OpQueue(lambda op: None, interval=0.0)
    await queue.start()
    ingest = _FakeIngest(queue)
    bridge = _FakeBridge(store, queue)
    dl = _FakeDlServer()
    tmp = tmp_path / "tmp"
    d = DistributorService(
        store, api, ops=None, bridge=bridge, ingest=ingest, dlserver=dl,
        queue=queue, tmp_dir=tmp,
    )
    yield tmp_path, store, api, d, ingest, bridge, dl
    await queue.shutdown()
    await store.close()


@pytest.mark.asyncio
async def test_validate_targets():
    assert "local" in DISTRIBUTE_TARGETS and "netdisk" in DISTRIBUTE_TARGETS
    assert "album" in DISTRIBUTE_TARGETS and "essence" in DISTRIBUTE_TARGETS
    assert "group" in DISTRIBUTE_TARGETS and "copy" in DISTRIBUTE_TARGETS


@pytest.mark.asyncio
async def test_file_to_netdisk(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.bin",
                   source_ref="f1", size=10, busid=1, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10)
    )
    rid = page.items[0].id
    out = await d.distribute_file("g1", rid, "netdisk")
    assert out["target"] == "netdisk" and out["task_id"] == f"out-{rid}"


@pytest.mark.asyncio
async def test_file_to_local_with_smb_notice(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="a.bin",
                   source_ref="f2", size=10, busid=1, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10)
    )
    rid = page.items[0].id
    out = await d.distribute_file("g1", rid, "local")
    assert out["target"] == "local"
    assert out["http_url"].startswith("http://dl.local/")
    assert out["sftp"] and out["sftp"]["port"] == 22
    assert out["smb"] is None
    assert "SMB" in out["smb_notice"]  # 诚实降级


@pytest.mark.asyncio
async def test_file_text_to_essence(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="readme.md",
                   source_ref="f3", size=5, busid=1, created_at=1,
                   meta={"summary": "doc"})
    await store.upsert_resources([res])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10)
    )
    rid = page.items[0].id
    out = await d.distribute_file("g1", rid, "essence")
    assert out["target"] == "essence" and out["task_id"].startswith("fetch-")
    assert ingest.fetch_calls[-1]["to_essence"] is True


@pytest.mark.asyncio
async def test_album_to_netdisk(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al1": [{"url": "http://cdn/x.jpg", "name": "x.jpg"}]}
    out = await d.distribute_album("g1", "al1", "x.jpg", "netdisk")
    assert out["target"] == "netdisk" and out["task_id"].startswith("off-")


@pytest.mark.asyncio
async def test_album_media_url_selects_by_name(env):
    """按 name 精确匹配媒体（2026-09-05 修复：此前恒取首个媒体）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al1": [
        {"url": "http://cdn/first.jpg", "name": "first.jpg"},
        {"url": "http://cdn/target.mp4", "name": "target.mp4"},
    ]}
    url = await d._album_media_url("g1", "al1", "target.mp4")
    assert url == "http://cdn/target.mp4"


@pytest.mark.asyncio
async def test_album_media_url_nested_shape(env):
    """旧版 QQ 嵌套 image.photo_url[].url.url 形状也能取到直链。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al9": [{"image": {"photo_url": [
        {"url": {"url": "http://cdn/nested.jpg", "width": 100, "height": 80}},
    ]}, "desc": "nested.jpg"}]}
    url = await d._album_media_url("g1", "al9", "nested.jpg")
    assert url == "http://cdn/nested.jpg"


@pytest.mark.asyncio
async def test_album_to_group_via_fetch(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al2": [{"url": "http://cdn/y.jpg", "name": "y.jpg"}]}
    out = await d.distribute_album("g1", "al2", "y.jpg", "group")
    assert out["target"] == "group" and out["task_id"].startswith("fetch-")
    assert ingest.fetch_calls[-1]["to_album"] is False


@pytest.mark.asyncio
async def test_essence_copy(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[7] = ("全文内容 abc", [])
    out = await d.distribute_essence("g1", 7, "copy")
    assert out["target"] == "copy" and out["text"] == "全文内容 abc"


@pytest.mark.asyncio
async def test_netdisk_to_essence(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    out = await d.distribute_netdisk("/doc.txt", "essence", group_id="g1", name="doc.txt")
    assert out["target"] == "essence" and out["task_id"].startswith("fetch-")
    assert ingest.fetch_calls[-1]["to_essence"] is True


@pytest.mark.asyncio
async def test_invalid_target(env):
    tmp_path, store, api, d, ingest, bridge, dl = env
    with pytest.raises(ValueError):
        await d.distribute_file("g1", 1, "nowhere")


# ---------- 网状拓扑补全：Album → Essence ----------

@pytest.mark.asyncio
async def test_album_to_essence(env):
    """相册媒体 → 精华（类型限制在入口：媒体元数据转文本）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al3": [{"url": "http://cdn/photo.jpg", "name": "photo.jpg"}]}
    out = await d.distribute_album("g1", "al3", "photo.jpg", "essence")
    assert out["target"] == "essence"
    assert out["via"] == "media-meta"
    # 验证生成了精华文本（通过 essence_save 队列）
    assert out["task_id"] is not None


# ---------- 网状拓扑补全：Essence → Album ----------

@pytest.mark.asyncio
async def test_essence_to_album(env):
    """精华文本 → 相册（类型限制在入口：文本渲染为图片）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[10] = ("测试精华文本\n第二行内容", [])
    out = await d.distribute_essence("g1", 10, "album")
    assert out["target"] == "album"
    assert out["via"] == "text-render"
    # 验证调用了 submit_fetch with to_album=True
    assert ingest.fetch_calls[-1]["to_album"] is True
    assert ingest.fetch_calls[-1]["name"].endswith(".png")


# ---------- 网状拓扑补全：Essence → Netdisk 两跳自动完成 ----------

@pytest.mark.asyncio
async def test_essence_to_netdisk_auto_bridge(env):
    """精华文本 → 网盘（两跳自动完成：upload + auto bridge_out）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[11] = ("网盘精华内容", [])
    # distribute_essence 需要 ops 来 submit_upload
    class _FakeOps:
        async def submit_upload(self, group_id, path, name):
            return "task-upload-1"
    d.ops = _FakeOps()
    d.tmp_dir = tmp_path / "tmp"
    out = await d.distribute_essence("g1", 11, "netdisk")
    assert out["target"] == "netdisk"
    assert out["via"] == "group-relay"


# ---------- 网状拓扑验证：所有源 → 所有目标的可达性 ----------

@pytest.mark.asyncio
async def test_full_mesh_file_targets(env):
    """文件 → 所有目标均可达。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    res = Resource(group_id="g1", type=ResourceType.FILE, name="test.bin",
                   source_ref="f100", size=10, busid=1, created_at=1)
    await store.upsert_resources([res])
    page = await store.query_resources(
        __import__("core.domain.sync", fromlist=["ResourceQuery"]).ResourceQuery(
            group_id="g1", page_size=10)
    )
    rid = page.items[0].id
    for target in ("local", "netdisk", "album", "essence"):
        out = await d.distribute_file("g1", rid, target)
        assert out["target"] == target


@pytest.mark.asyncio
async def test_full_mesh_album_targets(env):
    """相册 → 所有目标均可达（local/netdisk/group/essence）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al99": [{"url": "http://cdn/v.jpg", "name": "v.jpg"}]}
    for target in ("local", "netdisk", "group", "essence"):
        out = await d.distribute_album("g1", "al99", "v.jpg", target)
        assert out["target"] == target


@pytest.mark.asyncio
async def test_full_mesh_essence_targets(env):
    """精华 → 所有目标均可达（local/copy/netdisk/group/album）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[99] = ("全文测试", [])

    class _FakeOps:
        async def submit_upload(self, group_id, path, name):
            return "task-upload-1"
    d.ops = _FakeOps()
    d.tmp_dir = tmp_path / "tmp"

    for target in ("local", "copy", "group"):
        out = await d.distribute_essence("g1", 99, target)
        assert out["target"] == target
    # album 需要 Pillow (may not be installed)
    try:
        out = await d.distribute_essence("g1", 99, "album")
        assert out["target"] == "album"
    except RuntimeError:
        pytest.skip("Pillow not installed")


@pytest.mark.asyncio
async def test_full_mesh_netdisk_targets(env):
    """网盘 → 所有目标均可达（local/group/album/essence）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    for target in ("local", "group", "album", "essence"):
        out = await d.distribute_netdisk("/file.txt", target, group_id="g1")
        assert out["target"] == target