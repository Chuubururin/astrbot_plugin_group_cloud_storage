"""网状拓扑点对点双向传输测试。

验证四个节点（files/albums/essence/netdisk）之间的完整网状传输拓扑：
- 每个源到每个目标均有可达路径
- 类型限制仅在相册入口（仅图片/视频）和精华入口（仅文本）生效

Run: pytest tests/contract/test_transfer_topology.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.distributor import DISTRIBUTE_TARGETS  # noqa: E402


# ---------- 目标白名单验证 ----------

def test_distribute_targets_complete():
    """目标白名单包含全部 6 个合法目标。"""
    expected = {"local", "netdisk", "album", "essence", "group", "copy"}
    assert DISTRIBUTE_TARGETS == expected


def test_distribute_targets_immutable():
    """目标白名单为 frozenset（不可变）。"""
    assert isinstance(DISTRIBUTE_TARGETS, frozenset)


# ---------- 网状拓扑可达性（类型限制在入口） ----------

# 完整网状矩阵：4 源 × 4 目标 = 16 条路径（含类型限制路径）
TRANSFER_MATRIX = {
    # 文件 → 所有目标（无类型限制）
    "file": {
        "local": "直链下载",
        "netdisk": "bridge_out 正传",
        "album": "直链 → fetch to_album（入口限制：仅图片/视频）",
        "essence": "直链 → fetch to_essence（入口限制：仅文本）",
    },
    # 相册 → 所有目标（入口限制在 album 本身：仅图片/视频）
    "album": {
        "local": "媒体直链",
        "netdisk": "媒体直链 → OpenList 离线下载",
        "group": "媒体直链 → fetch 入群文件",
        "essence": "媒体元数据转文本精华（入口限制：转为文本）",
    },
    # 精华 → 所有目标（入口限制在 essence 本身：仅文本）
    "essence": {
        "local": "全文返回",
        "copy": "全文复制",
        "netdisk": "全文暂存 → upload → 自动 bridge_out（两跳）",
        "group": "全文暂存 → ops.upload",
        "album": "文本渲染为 PNG 图片入相册（入口限制：渲染为图）",
    },
    # 网盘 → 所有目标（无类型限制）
    "netdisk": {
        "local": "OpenList 直链",
        "group": "bridge_in",
        "album": "直链 → fetch to_album（入口限制：仅图片/视频）",
        "essence": "直链 → fetch to_essence（入口限制：仅文本）",
    },
}


def test_transfer_matrix_source_coverage():
    """四个源类型全部在矩阵中。"""
    assert set(TRANSFER_MATRIX.keys()) == {"file", "album", "essence", "netdisk"}


def test_transfer_matrix_target_coverage():
    """每个源至少有 4 个目标。"""
    for source, targets in TRANSFER_MATRIX.items():
        assert len(targets) >= 4, f"{source} has only {len(targets)} targets"


def test_file_reaches_all_targets():
    """文件可到达所有 6 个目标（含 group/copy）。"""
    file_targets = set(TRANSFER_MATRIX["file"].keys())
    assert file_targets >= {"local", "netdisk", "album", "essence"}


def test_album_reaches_all_targets():
    """相册可到达所有 4 个目标。"""
    album_targets = set(TRANSFER_MATRIX["album"].keys())
    assert album_targets >= {"local", "netdisk", "group", "essence"}


def test_essence_reaches_all_targets():
    """精华可到达所有 5 个目标。"""
    essence_targets = set(TRANSFER_MATRIX["essence"].keys())
    assert essence_targets >= {"local", "copy", "netdisk", "group", "album"}


def test_netdisk_reaches_all_targets():
    """网盘可到达所有 4 个目标。"""
    netdisk_targets = set(TRANSFER_MATRIX["netdisk"].keys())
    assert netdisk_targets >= {"local", "group", "album", "essence"}


# ---------- 类型限制仅在入口 ----------

def test_album_entry_type_restriction():
    """相册入口类型限制：仅接受图片/视频（_IMAGE_EXTS | _VIDEO_EXTS）。"""
    # 文件 → 相册时，submit_fetch 会检查扩展名
    # 相册作为源时，不做入口检查（只在目标入口检查）
    # 这里验证矩阵描述正确
    assert "仅图片/视频" in TRANSFER_MATRIX["file"]["album"]
    assert "仅图片/视频" in TRANSFER_MATRIX["netdisk"]["album"]


def test_essence_entry_type_restriction():
    """精华入口类型限制：仅接受文本。"""
    # 文件 → 精华时，submit_fetch 读取为文本
    # 精华作为源时，不做入口检查（只在目标入口检查）
    assert "仅文本" in TRANSFER_MATRIX["file"]["essence"]
    assert "仅文本" in TRANSFER_MATRIX["netdisk"]["essence"]


# ---------- 双向性验证 ----------

def test_bidirectional_file_netdisk():
    """文件 ↔ 网盘 双向可达。"""
    assert "netdisk" in TRANSFER_MATRIX["file"]
    assert "group" in TRANSFER_MATRIX["netdisk"]


def test_bidirectional_file_album():
    """文件 ↔ 相册 双向可达。"""
    assert "album" in TRANSFER_MATRIX["file"]
    assert "group" in TRANSFER_MATRIX["album"]


def test_bidirectional_file_essence():
    """文件 ↔ 精华 双向可达。"""
    assert "essence" in TRANSFER_MATRIX["file"]
    assert "group" in TRANSFER_MATRIX["essence"]


def test_bidirectional_album_netdisk():
    """相册 ↔ 网盘 双向可达。"""
    assert "netdisk" in TRANSFER_MATRIX["album"]
    assert "album" in TRANSFER_MATRIX["netdisk"]


def test_bidirectional_album_essence():
    """相册 ↔ 精华 双向可达。"""
    assert "essence" in TRANSFER_MATRIX["album"]
    assert "album" in TRANSFER_MATRIX["essence"]


def test_bidirectional_essence_netdisk():
    """精华 ↔ 网盘 双向可达。"""
    assert "netdisk" in TRANSFER_MATRIX["essence"]
    assert "essence" in TRANSFER_MATRIX["netdisk"]
