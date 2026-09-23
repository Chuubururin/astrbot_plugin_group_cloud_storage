"""回归测试：文件管理路径。

- M2 跨群分卷删除必须切到分卷所在群的账号作用域
- M10 分卷上传中途失败不得在 tmp_dir/vol_<id>/ 残留 .zip
- H3 分卷 upsert 不得把已 uploaded 的分片打回 pending（重放不得重传全部分片）
- M6 _do_upload/_do_delete 的静态条件必须抛 LOCAL_ERROR（不得白重放 3 次）
- M5 SFTP 传输必须有空闲超时，且超时归类为 TIMEOUT
- L3 transfer 静态条件（SSRF/scheme/超 max_bytes）必须抛 LOCAL_ERROR
- L4 converter 非法 convert_to 必须抛 LOCAL_ERROR
- 低危 download.py 不完整下载的显式标记
- 低危 converter.py ext 归一化（删除不可达分支后行为不变）
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from core.application.files import FileOpsService  # noqa: E402
from core.application.files import consts  # noqa: E402
from core.application.files.converter import ConverterService  # noqa: E402
from core.application.queue import OpQueue  # noqa: E402
from core.application.transfer import ProtocolAdapter, TransferService  # noqa: E402
from core.application.sync import ResourceSyncService  # noqa: E402
from core.domain.enums import (  # noqa: E402
    OneBotApiError,
    OneBotErrorKind,
    ResourceStatus,
    ResourceType,
)
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import GroupInfo, ResourceQuery, VolumeInfo  # noqa: E402
from core.opctx import account_var  # noqa: E402
from tests.fixtures.fake_onebot import FakeOneBotApi  # noqa: E402


@pytest.fixture
async def env(tmp_path):
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    api = FakeOneBotApi(tree={None: ([], [])})
    sync = ResourceSyncService(api, store)
    ops: FileOpsService | None = None
    queue = OpQueue(lambda op: ops.handle(op))
    await queue.start()
    ops = FileOpsService(api, store, queue, sync, tmp_dir=tmp_path / "tmp")
    yield tmp_path, store, api, queue, ops
    await queue.shutdown()
    await store.close()


async def _drain_op(queue, task_id, timeout=30.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await queue.status()
        recent = [r for r in st["recent"] if r["task_id"] == task_id]
        if recent:
            return recent[0]
        await asyncio_sleep()
    raise TimeoutError("op not finished")


async def asyncio_sleep():
    import asyncio

    await asyncio.sleep(0.05)


class _AccountAwareApi(FakeOneBotApi):
    """列目录只在「该群归属账号」下可见：模拟分卷落在非父资源账号的群。"""

    def __init__(self, *args, visible: dict[str, str], **kwargs):
        super().__init__(*args, **kwargs)
        self.visible = visible
        self.list_scopes: list[tuple[str, str]] = []

    async def list_group_root(self, group_id: str):
        self.list_scopes.append((str(group_id), account_var.get()))
        if self.visible.get(str(group_id)) != account_var.get():
            # 账号不对：云端目录列表失败（真实场景是会话/权限不匹配）
            raise OneBotApiError(
                OneBotErrorKind.LOCAL_ERROR, "get_group_root_files", "wrong account"
            )
        return await super().list_group_root(group_id)


async def _volume_resource(store, parent_group="g1", source_ref="vol1", name="big.bin"):
    await store.upsert_resources(
        [
            Resource(
                group_id=parent_group,
                type=ResourceType.FILE,
                name=name,
                source_ref=source_ref,
                size=20,
                created_at=1,
                meta={"volumes": True},
            )
        ]
    )
    page = await store.query_resources(
        ResourceQuery(group_id=parent_group, page_size=10)
    )
    return page.items[0].id


# ---------- M2 ----------


@pytest.mark.asyncio
async def test_delete_volume_parts_uses_each_groups_account(tmp_path):
    """M2：分卷落在其它账号的群时，删除路径必须切到该群的账号作用域。

    修复前：_resolve_file_ref 在错误账号下列目录失败被吞成 None -> continue
    跳过删除，但 remove_volumes + 软删父资源照常执行 -> 云端孤儿分卷。
    """
    store = SqliteMetaStore(tmp_path / "meta.db")
    await store.init()
    await store.upsert_groups(
        [
            GroupInfo(group_id="g1", account_id="10001"),
            GroupInfo(group_id="g2", account_id="20002"),
        ]
    )
    api = _AccountAwareApi(
        tree={
            None: (
                [
                    dict(
                        file_id="f1",
                        name="big.part01of02.zip",
                        size=10,
                        busid=1,
                        uploader_id="10001",
                        uploader_name="Alice",
                        upload_time=1700000000,
                    )
                ],
                [],
            )
        },
        visible={"g2": "20002"},
    )
    sync = ResourceSyncService(api, store)
    ops: FileOpsService | None = None
    queue = OpQueue(lambda op: ops.handle(op))
    await queue.start()
    ops = FileOpsService(api, store, queue, sync, tmp_dir=tmp_path / "tmp")
    try:
        rid = await _volume_resource(store)
        await store.insert_volumes(
            [
                VolumeInfo(
                    parent_resource_id="g1:file:vol1",
                    seq=1,
                    part_name="big.part01of02.zip",
                    size=10,
                    sha256="deadbeef",
                    status="uploaded",
                    group_id="g2",
                    source_ref="f1",
                    busid=1,
                )
            ]
        )
        api.calls.clear()
        tid = await ops.submit_delete("g1", rid)
        r = await _drain_op(queue, tid)
        assert r["state"] == "ok"
        # 分卷在 g2，必须由 g2 的账号（20002）解析并删除
        assert ("g2", "20002") in api.list_scopes, api.list_scopes
        assert "delete_group_file:g2:f1" in api.calls, api.calls
        detail = await store.get_resource_detail("g1", rid)
        assert detail["status"] == ResourceStatus.DELETED.value
    finally:
        await queue.shutdown()
        await store.close()


# ---------- M10 ----------


@pytest.mark.asyncio
async def test_volume_upload_failure_leaves_no_zip(env, monkeypatch):
    """M10：_do_volume_upload 中途抛错时，tmp_dir/vol_<parent>/ 必须被清掉。"""
    tmp_path, store, api, queue, ops = env
    monkeypatch.setattr(consts, "VOLUME_SIZE_BYTES", 4)  # 10 字节 -> 3 个分卷
    rid = await _volume_resource(store)
    src = tmp_path / "big.bin"
    src.write_bytes(b"0123456789")

    calls = {"n": 0}

    async def _flaky(group_id, file_path, name="", folder_id=None, folder="", upload_file=True):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OneBotApiError(
                OneBotErrorKind.LOCAL_ERROR, "upload_group_file", "upload failed"
            )

    monkeypatch.setattr(api, "upload_group_file", _flaky)
    op = SimpleNamespace(
        task_id="t1",
        kind="upload",
        target="g1",
        cancel=False,
        pause=False,
        payload={
            "parent_resource_id": "vol1",
            "parent_resource_id_full": "g1:file:vol1",
            "name": "big.bin",
        },
    )
    with pytest.raises(OneBotApiError):
        await ops._do_volume_upload(op, src, "big.bin", "vol1", None)

    cut_dir = tmp_path / "tmp" / "vol_vol1"
    assert not cut_dir.exists(), sorted(p.name for p in cut_dir.glob("*"))
    # 分卷行仍然保留（重试以 DB 行为准，不依赖本地切片）
    vols = await store.list_volumes("g1:file:vol1")
    assert len(vols) == 3
    assert rid  # 父资源仍在


# ---------- 低危：不完整下载的显式标记 ----------


@pytest.mark.asyncio
async def test_download_incomplete_sets_marker_header(env, monkeypatch):
    tmp_path, store, api, queue, ops = env
    rid = await _volume_resource(store)
    await store.insert_volumes(
        [
            VolumeInfo(
                parent_resource_id="g1:file:vol1",
                seq=1,
                part_name="big.part01of02.zip",
                size=10,
                status="uploaded",
                group_id="g1",
                source_ref="f1",
                busid=1,
            ),
            VolumeInfo(
                parent_resource_id="g1:file:vol1",
                seq=2,
                part_name="big.part02of02.zip",
                size=10,
                status="uploaded",
                group_id="g1",
                source_ref=None,
            ),
        ]
    )

    async def _fetch(self, url):
        return b"0123456789"

    monkeypatch.setattr(FileOpsService, "_fetch_bytes", _fetch)

    headers: dict[str, str] = {}
    out, name = await ops.download_info(
        "g1", rid, allow_incomplete=True, headers=headers
    )
    assert name == "big.bin"
    assert Path(out).read_bytes() == b"0123456789"
    assert headers.get("X-Cloud-Volume-Incomplete") == "1"
    assert headers.get("X-Cloud-Volume-Missing") == "2"

    # 完整下载不得带不完整标记
    await store.update_volume_fields("g1:file:vol1", 2, source_ref="f2", busid=1)
    full: dict[str, str] = {}
    await ops.download_info("g1", rid, headers=full)
    assert "X-Cloud-Volume-Incomplete" not in full


# ---------- 低危：converter ext 归一化 ----------


@pytest.mark.asyncio
async def test_converter_extension_normalisation(tmp_path, monkeypatch):
    """删除不可达的 startswith('.') 分支后行为不变：前导点已由 lstrip 去掉。"""
    conv = ConverterService(tmp_dir=tmp_path)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")

    async def _fake_copy(srcp, target):
        return target

    monkeypatch.setattr(conv, "_convert_video_copy", _fake_copy)
    for raw in (".mp4", "mp4", ".MP4", "..mp4"):
        out = await conv.convert(src, raw)
        assert out.name == "a.mp4", (raw, out)
    # 纯点 -> 归一化后为空 -> 明确报错（而不是静默产生一个空扩展名）
    with pytest.raises(ValueError, match="convert_to must be"):
        await conv.convert(src, "...")


# ---------- H3 ----------


@pytest.mark.asyncio
async def test_insert_volumes_preserves_uploaded_status(env):
    """H3：重放时 upsert 不得把已 uploaded 的分片打回 pending。"""
    _tmp_path, store, _api, _queue, _ops = env
    await store.insert_volumes(
        [
            VolumeInfo(
                parent_resource_id="g1:file:vol1",
                seq=1,
                part_name="big.part01of02.zip",
                size=10,
                status="uploaded",
                source_ref="f1",
                busid=1,
            )
        ]
    )
    # 重入管线：volumes 恒以 status="pending" 构造
    await store.insert_volumes(
        [
            VolumeInfo(
                parent_resource_id="g1:file:vol1",
                seq=1,
                part_name="big.part01of02.zip",
                size=10,
                status="pending",
            )
        ]
    )
    vol = (await store.list_volumes("g1:file:vol1"))[0]
    assert vol.status == "uploaded", "已 uploaded 的分片被 upsert 打回 pending"
    assert vol.source_ref == "f1"


@pytest.mark.asyncio
async def test_volume_upload_skips_uploaded_parts_on_replay(env, monkeypatch):
    """H3：重放（重试/暂停恢复）不得重传已 uploaded 的分片。

    修复前：insert_volumes 把 3 个 uploaded 分片全打回 pending，而 existing_vols
    在 upsert 之后才查 -> 跳过逻辑恒不生效 -> 全部分片重传。
    """
    tmp_path, store, api, queue, ops = env
    monkeypatch.setattr(consts, "VOLUME_SIZE_BYTES", 4)  # 10 字节 -> 3 个分卷
    await _volume_resource(store)
    src = tmp_path / "big.bin"
    src.write_bytes(b"0123456789")
    await store.insert_volumes(
        [
            VolumeInfo(
                parent_resource_id="g1:file:vol1",
                seq=seq,
                part_name=f"big.part{seq:02d}of03.zip",
                size=4,
                status="uploaded",
                source_ref=f"f{seq}",
                busid=1,
                group_id="g1",
            )
            for seq in (1, 2, 3)
        ]
    )
    api.calls.clear()
    op = SimpleNamespace(
        task_id="t1",
        kind="upload",
        target="g1",
        cancel=False,
        pause=False,
        replayed=True,  # 队列重放标记（重试 / 暂停恢复）
        payload={
            "parent_resource_id": "vol1",
            "parent_resource_id_full": "g1:file:vol1",
            "name": "big.bin",
        },
    )
    await ops._do_volume_upload(op, src, "big.bin", "vol1", None)
    uploads = [c for c in api.calls if c.startswith("upload_group_file")]
    assert uploads == [], f"重放时重传了已上传分片: {uploads}"
    vols = await store.list_volumes("g1:file:vol1")
    assert [v.status for v in vols] == ["uploaded"] * 3


# ---------- M6 ----------


def _op(kind: str, payload: dict, target: str = "g1"):
    return SimpleNamespace(
        task_id="t1", kind=kind, target=target, cancel=False, pause=False,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_upload_missing_folder_is_local_error(env):
    """M6：目标文件夹不存在是确定性条件，必须 LOCAL_ERROR（不得重放 3 次）。"""
    tmp_path, store, api, queue, ops = env
    src = tmp_path / "staged.bin"
    src.write_bytes(b"x")
    op = _op("upload", {
        "path": str(src), "name": "staged.bin", "folder_id": "不存在的文件夹"
    })
    with pytest.raises(OneBotApiError) as ei:
        await ops._do_upload(op)
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR


@pytest.mark.asyncio
async def test_delete_unresolvable_file_is_local_error(env):
    """M6：listing 无此文件且 payload 无 file_id，必须 LOCAL_ERROR（不得重放 3 次）。"""
    tmp_path, store, api, queue, ops = env
    await store.upsert_resources(
        [
            Resource(
                group_id="g1",
                type=ResourceType.FILE,
                name="gone.bin",
                source_ref="gone",
                size=10,
                created_at=1,
                meta={},
            )
        ]
    )
    page = await store.query_resources(
        ResourceQuery(group_id="g1", page_size=10)
    )
    rid = page.items[0].id
    # 云端 listing 已无此文件（FakeOneBotApi tree 为空），payload 也未带 file_id
    with pytest.raises(OneBotApiError) as ei:
        await ops._do_delete(_op("delete", {"id": rid}))
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR


# ---------- M5 ----------


def _transfer_service(tmp_path, **cfg):
    config = {"fetch_max_bytes": 1024, "fetch_timeout_sec": 5}
    config.update(cfg)
    return TransferService(
        MagicMock(), MagicMock(), tmp_path / "tmp", config=config
    )


@pytest.mark.asyncio
async def test_sftp_transfer_timeout_is_timeout_error(tmp_path, monkeypatch):
    """M5：SFTP 读超时（对端卡住）必须转成 TIMEOUT，且必须真的装上超时。"""
    import socket

    svc = _transfer_service(tmp_path, fetch_timeout_sec=7)
    adapter = svc._adapters["sftp"]
    armed: dict = {}

    class _Chan:
        def settimeout(self, timeout):
            armed["timeout"] = timeout

    class _Sftp:
        def get_channel(self):
            return _Chan()

        def stat(self, path):
            return None

        def get(self, remote, local):
            raise socket.timeout("peer stalled")

        def close(self):
            pass

    class _Ssh:
        def close(self):
            pass

    monkeypatch.setattr(adapter, "_conn", lambda target: (_Ssh(), _Sftp()))
    with pytest.raises(OneBotApiError) as ei:
        await adapter.get({"path": "/x/f.bin"}, tmp_path / "f.bin")
    assert ei.value.kind == OneBotErrorKind.TIMEOUT
    assert armed.get("timeout") == 7.0, "未给 SFTP 通道装上传输超时"


# ---------- L3 ----------


@pytest.mark.asyncio
async def test_fetch_ssrf_rejection_is_local_error(tmp_path):
    """L3：SSRF 拒斥是确定性条件 -> LOCAL_ERROR（且保留 ValueError 旧契约）。"""
    svc = _transfer_service(tmp_path)  # fetch_allow_private_address=False
    with pytest.raises(OneBotApiError) as ei:
        await svc.download_to("http://127.0.0.1:6186/x.bin", tmp_path / "x.bin")
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert isinstance(ei.value, ValueError)  # webapi/security 旧契约


@pytest.mark.asyncio
async def test_fetch_unsupported_scheme_is_local_error(tmp_path):
    """L3：不支持的 scheme -> LOCAL_ERROR（且保留 ValueError 旧契约）。"""
    svc = _transfer_service(tmp_path)
    with pytest.raises(OneBotApiError) as ei:
        await svc.download_to("ldap://h/x", tmp_path / "x")
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert isinstance(ei.value, ValueError)


@pytest.mark.asyncio
async def test_fetch_over_max_bytes_is_local_error(tmp_path):
    """L3：超 max_bytes 的兜底拒绝 -> LOCAL_ERROR（不得重放 3 次）。"""
    svc = _transfer_service(tmp_path, fetch_max_bytes=4)

    class _Big(ProtocolAdapter):
        scheme = "smb"

        async def get(self, target, dest):
            dest.write_bytes(b"12345")
            return 5

    svc._adapters["smb"] = _Big(4, 5.0)
    dest = tmp_path / "big.bin"
    with pytest.raises(OneBotApiError, match="max bytes") as ei:
        await svc.download_to("smb://h/share/big.bin", dest)
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert not dest.exists()


# ---------- L4 ----------


@pytest.mark.asyncio
async def test_converter_invalid_target_is_local_error(tmp_path):
    """L4：非法 convert_to（空/不支持）-> LOCAL_ERROR，且保留 ValueError 契约。"""
    conv = ConverterService(tmp_dir=tmp_path)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    with pytest.raises(OneBotApiError) as ei:
        await conv.convert(src, "...")  # 归一化后为空
    assert ei.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert isinstance(ei.value, ValueError)
    with pytest.raises(OneBotApiError) as ei2:
        await conv.convert(src, "avi")  # 不支持的扩展名
    assert ei2.value.kind == OneBotErrorKind.LOCAL_ERROR
    assert isinstance(ei2.value, ValueError)
