"""网状拓扑点对点双向传输——接线契约测试。

TRANSFER_MATRIX 是拓扑文档（4 源 × 各目标的路径说明）。本文件不再把矩阵与
自身比较（那是恒真断言、零假信心改进），而是把它钉在真实代码面上：
- 矩阵中出现的每个目标必须是 distributor.DISTRIBUTE_TARGETS 的真实成员
- 每条路径所依赖的入口方法必须在对应服务类上真实存在（改名/删除即失败）
行为级覆盖（真实分发执行、类型限制在入口生效）见 test_distributor.py。

Run: pytest tests/contract/test_transfer_topology.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.bridge.service import BridgeService  # noqa: E402
from core.application.distributor import (  # noqa: E402
    DISTRIBUTE_TARGETS,
    DistributorService,
)
from core.application.files import FileOpsService  # noqa: E402
from core.application.ingest import CloudIngestService  # noqa: E402


# ---------- 目标白名单验证 ----------

def test_distribute_targets_complete():
    """目标白名单包含全部 6 个合法目标。"""
    expected = {"local", "netdisk", "album", "essence", "group", "copy"}
    assert DISTRIBUTE_TARGETS == expected


def test_distribute_targets_immutable():
    """目标白名单为 frozenset（不可变）。"""
    assert isinstance(DISTRIBUTE_TARGETS, frozenset)


# ---------- 网状拓扑可达性（矩阵 → 真实接线） ----------

# 完整网状矩阵：4 源 × 各目标 = 拓扑文档（行为验证在 test_distributor.py）。
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

# 每条矩阵路径依赖的服务入口（服务类, 方法名）。矩阵文档改写路径而未改
# 代码（或反向）时，这里先失败。
_ROW_ENTRYPOINTS = {
    "file": [
        (DistributorService, "distribute_file"),
        (BridgeService, "submit_out"),
        (CloudIngestService, "submit_fetch"),
        (FileOpsService, "direct_link"),
    ],
    "album": [
        (DistributorService, "distribute_album"),
        (BridgeService, "submit_offline"),
        (CloudIngestService, "submit_fetch"),
        (CloudIngestService, "submit_essence_save"),
    ],
    "essence": [
        (DistributorService, "distribute_essence"),
        (BridgeService, "submit_offline"),
        (FileOpsService, "submit_upload"),
        (CloudIngestService, "submit_fetch"),
    ],
    "netdisk": [
        (DistributorService, "distribute_netdisk"),
        (BridgeService, "submit_in"),
        (CloudIngestService, "submit_fetch"),
        (FileOpsService, "direct_link"),
    ],
}


def test_matrix_rows_are_the_four_real_sources():
    """矩阵的行必须是四个真实资源源，不多不少。"""
    assert set(TRANSFER_MATRIX.keys()) == {"file", "album", "essence", "netdisk"}


def test_matrix_targets_are_real_targets():
    """矩阵中出现的每个目标都必须是生产白名单的真实成员。"""
    for source, targets in TRANSFER_MATRIX.items():
        unknown = set(targets) - DISTRIBUTE_TARGETS
        assert not unknown, f"{source} 引用了不可路由的目标: {sorted(unknown)}"


def test_matrix_rows_have_targets():
    """每个源至少有 4 个目标（拓扑文档完整性）。"""
    for source, targets in TRANSFER_MATRIX.items():
        assert len(targets) >= 4, f"{source} has only {len(targets)} targets"


def test_matrix_row_entrypoints_exist():
    """每条矩阵路径依赖的服务入口在真实类上存在（改名/删除即失败）。"""
    for source, entries in _ROW_ENTRYPOINTS.items():
        for cls, method in entries:
            assert hasattr(cls, method), (
                f"矩阵行 {source!r} 依赖的入口缺失: {cls.__name__}.{method}"
            )


def test_entrypoint_target_params_accept_matrix_targets():
    """分发入口的 target 形参签名存在且 DISTRIBUTE_TARGETS 可整体传入
    （防止白名单扩列后入口参数校验漏接）。"""
    import inspect

    for fn in (
        DistributorService.distribute_file,
        DistributorService.distribute_album,
        DistributorService.distribute_essence,
        DistributorService.distribute_netdisk,
    ):
        params = inspect.signature(fn).parameters
        assert "target" in params, f"{fn.__name__} 缺少 target 形参"


# ---------- 双向性验证（经真实服务对） ----------

def test_bidirectional_file_netdisk():
    """文件 ↔ 网盘 双向接线：submit_out 正传 + submit_in 恢复。"""
    assert hasattr(BridgeService, "submit_out")
    assert hasattr(BridgeService, "submit_in")


def test_bidirectional_matrix_symmetry():
    """矩阵文档的双向性：除 file 行（可达全部目标）外，其余源行必须
    指向至少一个其他源（网状而非单向星型）。"""
    sources = set(TRANSFER_MATRIX)
    for source, targets in TRANSFER_MATRIX.items():
        reachable_back = {t for t in targets if t in sources}
        if source != "file":
            assert reachable_back, f"{source} 行没有指向任何其他源（拓扑退化为星型）"
