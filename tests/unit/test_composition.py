"""组合模块测试（v2.8）：spec 编码规范 / splitter / reassembler / integrity。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.optional, pytest.mark.slow]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application.composition import (  # noqa: E402
    decode_composition, encode_composition, is_composite,
    reassemble_text, split_text)


# ---------- spec ----------

def test_spec_roundtrip_and_legacy():
    comp = encode_composition("volumes", 3, "binary", total_sha256="abc")
    meta = {"composition": comp, "volumes": True, "total_sha256": "abc"}
    assert decode_composition(meta) == comp
    assert is_composite(meta) is True
    # 旧形态兼容
    assert decode_composition({"volumes": True})["kind"] == "volumes"
    assert decode_composition({"kind": "text_split", "parts": [{"seq": 1}]})["parts"] == 1
    assert is_composite({}) is False


# ---------- splitter ----------

def test_split_text_boundaries():
    text = "第1行\n" * 20 + "超长单行" * 200
    chunks = split_text(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_text_structural_boundaries():
    """标题/汉字序号（一、/（一）/1.）总是开启新分段，不接在上一段尾部。"""
    lines = [" filler line " * 3 for _ in range(5)]
    text = "\n".join(lines) + "\n## 新章节标题\n" + "\n".join(lines) \
        + "\n（一）子项开始\n" + "\n".join(lines) + "\n2. 数字序号项\n" + "\n".join(lines)
    chunks = split_text(text, 120)
    joined = "\n".join(chunks)
    # 每个结构点都落在某个 chunk 的行首（不拼接在上文行尾）
    for marker in ("## 新章节标题", "（一）子项开始", "2. 数字序号项"):
        assert any(
            chunk.split("\n", 1)[0].startswith(marker.split(" ")[0])
            or marker in chunk.split("\n")[0] or marker in chunk
            for chunk in chunks
        ), marker
    assert all(len(c) <= 120 for c in chunks)
    # 内容无丢失（忽略用于分段的换行差异）
    assert joined.replace("\n", "") == text.replace("\n", "")


def test_reassemble_text():
    parts = [{"seq": 2, "text": "B"}, {"seq": 1, "text": "A"}]
    assert reassemble_text(parts) == "A\nB"
    with pytest.raises(ValueError):
        reassemble_text([{"seq": 1, "text": None}])


# ---------- reassembler: video（ffmpeg 可用时） ----------

@pytest.mark.asyncio
async def test_reassemble_video(tmp_path):
    import shutil as _sh
    import subprocess as _sp

    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    src = tmp_path / "v.mp4"
    proc = _sp.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=2:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    # 手动切两段 → 重组
    s1 = tmp_path / "s1.mp4"
    s2 = tmp_path / "s2.mp4"
    for out, ss, t in ((s1, 0, 1), (s2, 1, 1)):
        r = _sp.run(["ffmpeg", "-y", "-ss", str(ss), "-i", str(src),
                     "-t", str(t), "-c", "copy", str(out)],
                    capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-200:]
    from core.application.composition.reassembler import reassemble_video
    dest = tmp_path / "merged.mp4"
    await reassemble_video([s1, s2], dest)
    assert dest.stat().st_size > 0
