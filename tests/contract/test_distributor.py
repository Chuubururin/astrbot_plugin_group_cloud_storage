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
        self.proxies: list[tuple[str, str]] = []

    def download_url(self, group_id, rid):
        self.calls.append(f"url:{group_id}:{rid}")
        return f"http://dl.local/{group_id}/{rid}"

    def sftp_info(self):
        return {"host": "127.0.0.1", "port": 22, "user": "cloud", "password": "x"}

    def register_staged(self, path, name):
        self.calls.append(f"staged:{name}")
        return {"http_url": f"http://dl.local/staged/{name}", "sftp": None}

    def register_proxy(self, url, name):
        self.proxies.append((url, name))
        return f"http://dl.local/proxy/{len(self.proxies)}"


class _FakeBridge:
    def __init__(self, store, queue):
        self.store = store
        self.queue = queue
        self.out_tasks: list[str] = []
        self.client = _FakeOpenList()
        self._dst_dir = "/smb"
        self._dst_template = "{group_id}/{filename}"
        self.offline_paths: list[str] = []

    def _render_dst(self, dst_dir, group_id, filename):
        relative = self._dst_template.replace("{group_id}", group_id)
        relative = relative.replace("{filename}", filename).strip("/")
        dst_base = dst_dir.rstrip("/")
        if "/" in relative:
            dir_part = relative.rsplit("/", 1)[0]
            remote_dir = f"{dst_base}/{dir_part}"
        else:
            remote_dir = dst_base
        return remote_dir, f"{dst_base}/{relative}"

    async def submit_out(self, group_id, rid, *, dst_dir=None, force=False):
        tid = f"out-{rid}"
        self.out_tasks.append(tid)
        return tid

    async def submit_in(self, path, *, group_id):
        return f"in-{path}"


class _FakeOpenList:
    def __init__(self):
        self._offline_paths: list[str] = []
        self._offline_urls: list[list[str]] = []
        self._mkdirs: list[str] = []

    async def submit_offline_download(self, urls, path):
        self._offline_paths.append(path)
        self._offline_urls.append(list(urls))
        return [type("T", (), {"id": f"off-{len(urls)}"})()]

    async def mkdir(self, path):
        self._mkdirs.append(path)

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
    """相册→网盘：目录 = openlist_dst_dir + {group_id}/{filename} 模板渲染
    （与 bridge_out 的 _render_dst 同源；2026-09-07 修复硬编码 "/"，2026-09-12
    修复坏链#17 裸 _dst_dir 不带模板导致媒体堆在挂载根目录）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al1": [{"url": "http://cdn/x.jpg", "name": "x.jpg"}]}
    out = await d.distribute_album("g1", "al1", "x.jpg", "netdisk")
    assert out["target"] == "netdisk" and out["task_id"].startswith("off-")
    client = d._bridge_client(bridge)
    # 坏链#17：目标目录含群目录段（模板渲染），且提交前幂等建目录
    assert client._offline_paths == ["/smb/g1"]
    assert client._mkdirs == ["/smb/g1"]


@pytest.mark.asyncio
async def test_album_to_netdisk_proxy_carries_name(env):
    """坏链#18：QQ CDN 直链 URL 尾段是规格名（/0 /400 /800…），OpenList
    离线下载按 URL 尾段命名，落盘变成 "800"。修复后经 dlserver 代理，
    由 Content-Disposition 注入真实文件名。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al12": [
        {"image": {"name": "照片.png", "photoUrls": [
            {"spec": 6, "url": {"url": "http://cdn/photo/800?ek=1"}},
        ]}, "desc": "照片.png"},
    ]}
    out = await d.distribute_album("g1", "al12", "照片.png", "netdisk")
    assert out["target"] == "netdisk"
    # 提交给 OpenList 的 URL 是代理 URL，不是 CDN 原链
    client = d._bridge_client(bridge)
    assert dl.proxies, "register_proxy must be called"
    cdn_url, proxied_name = dl.proxies[0]
    assert cdn_url == "http://cdn/photo/800?ek=1"
    assert proxied_name == "照片.png"


@pytest.mark.asyncio
async def test_album_to_netdisk_without_proxy_appends_name(env):
    """无 dlserver 时降级：URL 尾段为纯数字/空则追加真实文件名，
    避免落盘名退化为规格名。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    d.dlserver = None
    api.album_media = {"g1:al13": [{"url": "http://cdn/photo/800", "name": "照片.png"}]}
    out = await d.distribute_album("g1", "al13", "照片.png", "netdisk")
    assert out["target"] == "netdisk"
    client = d._bridge_client(bridge)
    assert client._offline_paths == ["/smb/g1"]
    # 核心断言：提交给 OpenList 的 URL 已追加真实文件名（尾段 "800" 是规格名）
    assert client._offline_urls == [["http://cdn/photo/800/照片.png"]]

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
async def test_album_media_url_qq_image_name_no_ext(env):
    """QQ NT 形状 name 在 image.name 且去掉扩展名（2026-09-11 真机坏链：
    匹配落空后静默回退首个媒体，把另一张照片发出去）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al19": [
        {"type": 0, "image": {"name": "lossy_probe_final", "photoUrls": [
            {"url": {"url": "http://cdn/LOSSY/0?w5=129&h5=122", "width": 129, "height": 122}},
        ]}},
        {"type": 0, "image": {"name": "0d0629a19a68c9d3cd6166d8__logo", "photoUrls": [
            {"url": {"url": "http://cdn/HIRES/800?w5=415&h5=415", "width": 415, "height": 415}},
            {"url": {"url": "http://cdn/HIRES/0?w5=415&h5=415", "width": 415, "height": 415}},
        ]}},
    ]}
    url = await d._album_media_url("g1", "al19", "0d0629a19a68c9d3cd6166d8__logo.png")
    assert "HIRES" in url


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
async def test_album_media_url_nt_camel_shape(env):
    """QQ NT 相册驼峰形状（image.photoUrls/defaultUrl）也能取到直链
    （2026-09-07 修复：此前只认蛇形 photo_url，全部分发报 url unavailable）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al10": [
        {"image": {"name": "pic.png", "photoUrls": [
            {"spec": 5, "url": {"url": "http://cdn/nt-640.png", "width": 0, "height": 0}},
            {"spec": 1, "url": {"url": "http://cdn/nt-800.png", "width": 0, "height": 0}},
        ], "defaultUrl": {"url": "http://cdn/nt-default.png"}},
        "desc": "pic.png"},
        # 视频条目：videoUrl[] + 平铺播放 url；封面在 video.cover.photoUrls
        {"video": {"id": "v1", "url": "http://cdn/nt-video.mp4",
                   "cover": {"photoUrls": [{"url": {"url": "http://cdn/nt-cover.jpg"}}]}},
         "name": "clip.mp4"},
    ]}
    assert await d._album_media_url("g1", "al10", "pic.png") == "http://cdn/nt-640.png"
    assert await d._album_media_url("g1", "al10", "clip.mp4") == "http://cdn/nt-video.mp4"
    # 整个媒体列表都无蛇形 photo_url 时，图片经 defaultUrl 兜底也可取到
    api.album_media = {"g1:al11": [{"image": {
        "photoUrls": [{"url": {"url": "http://cdn/only-spec.png"}}],
    }}]}
    assert await d._album_media_url("g1", "al11", "") == "http://cdn/only-spec.png"


@pytest.mark.asyncio
async def test_album_media_url_picks_largest_spec(env):
    """多规格列表按像素面积取最大（镜像前端 byAreaDesc）。

    2026-09-11 修复：QQ NT 相册 photoUrls 小图在前，旧实现取第一项
    导致相册分发落盘 2659 字节缩略图而非原图（真机坏链）。
    """
    tmp_path, store, api, d, ingest, bridge, dl = env
    api.album_media = {"g1:al14": [
        {"image": {"name": "照片.png", "photoUrls": [
            {"spec": 6, "url": {"url": "http://cdn/photo/0", "width": 415, "height": 415}},
            {"spec": 1980, "url": {"url": "http://cdn/photo/800", "width": 415, "height": 415}},
            {"spec": 4, "url": {"url": "http://cdn/photo/400", "width": 400, "height": 400}},
        ]}, "desc": "照片.png"},
        # 视频多规格同理：720p 在前、1080p 在后，应取 1080p
        {"video": {"name": "clip.mp4", "videoUrl": [
            {"url": {"url": "http://cdn/v720.mp4", "width": 1280, "height": 720}},
            {"url": {"url": "http://cdn/v1080.mp4", "width": 1920, "height": 1080}},
        ]}, "name": "clip.mp4"},
    ]}
    assert await d._album_media_url("g1", "al14", "照片.png") == "http://cdn/photo/0"
    assert await d._album_media_url("g1", "al14", "clip.mp4") == "http://cdn/v1080.mp4"
    # 无尺寸信息的规格列表保持取第一项（rank 全 0，max 稳定取首个）
    api.album_media = {"g1:al15": [{"image": {"photoUrls": [
        {"url": {"url": "http://cdn/a.png"}},
        {"url": {"url": "http://cdn/b.png"}},
    ]}}]}
    assert await d._album_media_url("g1", "al15", "") == "http://cdn/a.png"
    # QQ lloc 变体的 w5×h5（URL 内真实像素）优先于一切（真机：同一张图有
    # 高清变体 w5=415 与低清孪生 w5=129，低清变体任何尾段都只有 2659B；
    # 声明 width/height 在低清变体上还是旧值）。变体不同 → w5 高者胜。
    # 同变体（w5 相同）内：URL 尾段规格决定——/0 是 QQ 原图规格，胜过 /800
    api.album_media = {"g1:al16": [{"image": {"name": "照片.png", "photoUrls": [
        {"spec": 5, "url": {"url": "http://cdn/photo/400?ek=1&w5=129&h5=122", "width": 4000, "height": 4000}},
        {"spec": 6, "url": {"url": "http://cdn/photo/0?ek=1&w5=415&h5=415", "width": 0, "height": 0}},
        {"spec": 1, "url": {"url": "http://cdn/photo/800?ek=1&w5=415&h5=415", "width": 415, "height": 415}},
    ]}, "desc": "照片.png"}]}
    assert await d._album_media_url("g1", "al16", "照片.png") == "http://cdn/photo/0?ek=1&w5=415&h5=415"
    # 同变体（w5 相同）内：URL 尾段规格决定（/800 原图语义 > /400），声明宽高仅兜底
    api.album_media = {"g1:al17": [{"image": {"photoUrls": [
        {"url": {"url": "http://cdn/photo/400?ek=1&w5=415&h5=415", "width": 4000, "height": 4000}},
        {"url": {"url": "http://cdn/photo/800?ek=1&w5=415&h5=415", "width": 100, "height": 100}},
    ]}}]}
    assert await d._album_media_url("g1", "al17", "") == "http://cdn/photo/800?ek=1&w5=415&h5=415"
    # 非 QQ 尾段（文件名式 URL）rank=0，靠声明面积兜底
    api.album_media = {"g1:al18": [{"image": {"photoUrls": [
        {"url": {"url": "http://cdn/thumb_small.png", "width": 10, "height": 10}},
        {"url": {"url": "http://cdn/thumb_big.png", "width": 800, "height": 600}},
    ]}}]}
    assert await d._album_media_url("g1", "al18", "") == "http://cdn/thumb_big.png"


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


# ---------- 网状拓扑补全：Essence → Netdisk（坏链#24 修复后两形态） ----------

@pytest.mark.asyncio
async def test_essence_to_netdisk_text_offline(env):
    """精华文本 → 网盘（主路径：staged 直链 + OpenList 离线下载）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[11] = ("网盘精华内容", [])
    d.tmp_dir = tmp_path / "tmp"
    out = await d.distribute_essence("g1", 11, "netdisk")
    assert out["target"] == "netdisk"
    assert out["via"] == "text-offline"
    assert out["task_id"].startswith("off-")
    # 目录按 {group_id}/{filename} 模板渲染（与 bridge_out 同源）
    assert bridge.client._mkdirs == ["/smb/g1"]
    assert bridge.client._offline_paths == ["/smb/g1"]
    # 离线下载吃的是 dlserver staged 直链（以精华显示名对外提供），
    # 而非 file:// 本地路径
    assert dl.calls == ["staged:essence_11.txt"]


@pytest.mark.asyncio
async def test_essence_to_netdisk_relay_fallback(env):
    """精华文本 → 网盘（回退：无下载服务时仅入群文件，如实注明手动转存）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[12] = ("回退路径内容", [])
    d.tmp_dir = tmp_path / "tmp"
    dl.enabled = False

    class _FakeOps:
        async def submit_upload(self, group_id, path, name):
            return "task-upload-1"
    d.ops = _FakeOps()
    out = await d.distribute_essence("g1", 12, "netdisk")
    assert out["target"] == "netdisk"
    assert out["via"] == "group-relay"
    assert "手动" in out["note"]


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


@pytest.mark.asyncio
async def test_essence_to_album_requires_pillow(env):
    """精华 → 相册（渲染为图片入相册）需要 Pillow；未安装时环境级跳过，
    其余失败照常报错（不再吞 RuntimeError）。"""
    pytest.importorskip("PIL")
    tmp_path, store, api, d, ingest, bridge, dl = env
    ingest.essence_texts[99] = ("全文测试", [])

    class _FakeOps:
        async def submit_upload(self, group_id, path, name):
            return "task-upload-1"
    d.ops = _FakeOps()
    d.tmp_dir = tmp_path / "tmp"

    out = await d.distribute_essence("g1", 99, "album")
    assert out["target"] == "album"


@pytest.mark.asyncio
async def test_full_mesh_netdisk_targets(env):
    """网盘 → 所有目标均可达（local/group/album/essence）。"""
    tmp_path, store, api, d, ingest, bridge, dl = env
    for target in ("local", "group", "album", "essence"):
        out = await d.distribute_netdisk("/file.txt", target, group_id="g1")
        assert out["target"] == target