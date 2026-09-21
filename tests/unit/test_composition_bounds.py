"""组合模块边界回归：split_text 下界保护 / 结构性行超长硬切 /
reassemble_volumes 流式落盘（内存有界 + 原子）/ concat 清单清理。"""

from __future__ import annotations

import hashlib
import sys
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.composition.reassembler import (  # noqa: E402
    reassemble_video,
    reassemble_volumes,
)
from core.application.composition.splitter import split_text  # noqa: E402


# ---------- split_text ----------

def test_split_text_non_positive_limit_terminates():
    """M12 回归：limit <= 0 不得死循环。

    essence_chunk_size 为负数时会原样透传给 split_text；修复前 _best_cut 返回
    cut <= 0 -> line 不再变短 -> 死循环 + chunks 无界增长（同步函数，卡死整个
    事件循环）。
    """
    chunks = split_text("a" * 50, 0)
    assert len(chunks) == 50
    assert all(len(c) == 1 for c in chunks)
    assert "".join(chunks) == "a" * 50
    assert "".join(split_text("b" * 7, -3)) == "b" * 7


def test_split_text_structural_line_over_limit_is_hard_cut():
    """结构性行（标题/中文序号）超 limit 时仍需硬切。

    修复前 `buf = line` 绕过硬切，_flush() 会产出 > limit 的分块（QQ 精华被截断）。
    """
    text = "前言\n" + "## " + "长标题" * 60
    chunks = split_text(text, 50)
    assert all(len(c) <= 50 for c in chunks), [len(c) for c in chunks]
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# ---------- reassemble_volumes ----------

def _part(tmp_path: Path, name: str, data: bytes) -> dict:
    p = tmp_path / name
    p.write_bytes(data)
    return {"path": str(p), "sha256": hashlib.sha256(data).hexdigest(), "seq": 1}


def _leftovers(tmp_path: Path) -> list[str]:
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".reassembling")]


def test_reassemble_volumes_streams_and_verifies(tmp_path):
    a = b"A" * (1 << 16) + b"tail-a"
    b = b"B" * 1000
    parts = [_part(tmp_path, "p1.bin", a), _part(tmp_path, "p2.bin", b)]
    dest = tmp_path / "out.bin"
    digest = reassemble_volumes(parts, dest)
    assert dest.read_bytes() == a + b
    assert digest == hashlib.sha256(a + b).hexdigest()
    assert _leftovers(tmp_path) == []


def test_reassemble_volumes_keeps_dest_atomic_on_mismatch(tmp_path):
    """校验失败不得写穿目标文件（先写临时文件 + 原子 rename）。"""
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"previous")
    bad = [{"path": str(_part(tmp_path, "bad.bin", b"xyz")["path"]),
            "sha256": "0" * 64}]
    with pytest.raises(ValueError):
        reassemble_volumes(bad, dest)
    assert dest.read_bytes() == b"previous"
    assert _leftovers(tmp_path) == []


def test_reassemble_volumes_memory_bounded(tmp_path):
    """M19 回归：单卷内存占用不随卷大小增长。

    修复前把整卷读进 chunks_buf 后才落盘（64KB 分块注释与实际不符），8MB 卷
    峰值 ~8MB；流式实现应在 1MB 量级。
    """
    big = b"Z" * (8 << 20)
    parts = [_part(tmp_path, "big.bin", big)]
    dest = tmp_path / "big.out"
    tracemalloc.start()
    try:
        reassemble_volumes(parts, dest)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert dest.stat().st_size == len(big)
    assert peak < (4 << 20), f"单卷峰值内存 {peak} 字节（应为流式，修复前 ~8MB）"


# ---------- reassemble_video ----------

@pytest.mark.asyncio
async def test_reassemble_video_removes_concat_manifest_on_failure(tmp_path, monkeypatch):
    """concat 失败时临时清单不得残留（无需真实 ffmpeg）。

    注入两个接缝，不再依赖机器上是否装了 ffmpeg：``shutil.which`` 假装 ffmpeg
    存在（否则 reassemble_video 在写清单之前就 raise，
    ``pytest.raises(ValueError)`` 会假性通过、清单从未创建——旧版靠
    ``pytest.skip`` 把这种假绿整条盖掉了）；``subprocess.run`` 返回非零码，
    制造一次真实的 concat 失败。断言失败确实发生在清单写入之后，且 finally
    把它清掉了。
    """
    from core.application.composition import reassembler

    monkeypatch.setattr(
        reassembler, "shutil", SimpleNamespace(which=lambda _name: "/usr/bin/ffmpeg")
    )

    seen: dict = {}

    class _Proc:
        returncode = 1
        stderr = "boom"

    def _fake_run(cmd, **_kwargs):
        # cmd 里的 -i 参数就是 concat 清单；它必须已经落盘，
        # 否则本次失败发生在写清单之前，用例就失去意义。
        seen["manifest_existed"] = Path(cmd[cmd.index("-i") + 1]).exists()
        return _Proc()

    monkeypatch.setattr(reassembler, "subprocess", SimpleNamespace(run=_fake_run))

    dest = tmp_path / "merged.mp4"
    with pytest.raises(ValueError, match="ffmpeg concat failed"):
        await reassemble_video([tmp_path / "missing.mp4"], dest)
    assert seen["manifest_existed"] is True, "失败必须发生在清单写入之后"
    assert not (tmp_path / "merged_concat.txt").exists()
    assert not dest.exists()
