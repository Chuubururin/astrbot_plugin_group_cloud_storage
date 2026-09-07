"""格式转换服务契约（W2-B，2026-09-02 ADR-0009）：
ConverterService 视频重封装/重编码、图片重编码、文本归一、目标校验；
上传 prepare 的 convert_to 参数白名单。

ffmpeg 不可用或输入非媒体时按 ValueError 报告（任务层明示降级）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.optional, pytest.mark.slow]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.files.converter import ConverterService  # noqa: E402


@pytest.fixture
def conv(tmp_path):
    return ConverterService(tmp_dir=tmp_path)


def _has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def test_target_validation(conv):
    assert conv.target_supported(".mp4")
    assert conv.target_supported(".mkv")
    assert conv.target_supported(".png")
    assert conv.target_supported(".webm")
    assert not conv.target_supported(".zip")
    assert not conv.target_supported(".gif")
    assert conv.is_video_ext(".mp4") and not conv.is_video_ext(".png")
    assert conv.is_image_ext(".jpg") and not conv.is_image_ext(".mp4")


def test_normalize_text(conv):
    raw = "\ufeff第一行\r\n第二行\r第三行\n"
    out = conv.normalize_text(raw)
    assert not out.startswith("\ufeff")
    assert out == "第一行\n第二行\n第三行\n"


@pytest.mark.asyncio
async def test_text_convert_returns_original(tmp_path, conv):
    src = tmp_path / "doc.txt"
    src.write_text("正文", encoding="utf-8")
    # 无目标扩展名 → ValueError（明确拒绝）
    with pytest.raises(ValueError):
        await conv.convert(src, "")


@pytest.mark.asyncio
async def test_video_remux_mp4_to_mkv(tmp_path, conv):
    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available")
    import subprocess

    src = tmp_path / "v.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    out = await conv.convert(src, "mkv")
    assert out.suffix == ".mkv" and out.exists() and out.stat().st_size > 0
    assert out.parent == src.parent  # 同目录临时产物（调用方清理）


@pytest.mark.asyncio
async def test_image_reencode_png_to_jpg(tmp_path, conv):
    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available")
    import subprocess

    src = tmp_path / "i.png"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
         "-frames:v", "1", "-c:v", "png", str(src)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-300:]
    out = await conv.convert(src, "jpg")
    assert out.suffix == ".jpg" and out.exists() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_invalid_target_rejected(tmp_path, conv):
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        await conv.convert(src, "zip")

@pytest.mark.asyncio
async def test_lossy_compress_image(tmp_path, conv):
    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available")
    import subprocess

    src = tmp_path / "large.png"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240",
         "-frames:v", "1", "-c:v", "png", str(src)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    out = await conv.compress(src)
    assert out.exists() and out.stat().st_size > 0
    assert out.name != src.name or out != src  # never mutates the input in place


def test_normalize_convert_to_whitelist():
    """2026-09-03 核对补写：fetch / netdisk/distribute 共用白名单 helper。

    回归背景：结构重构期 webapi.py 引用未定义的 _normalize_convert_to，
    真机 E2E 捕获 NameError → 500（本应 400）。本用例为守门：helper 缺失即 import 失败。
    """
    import webapi  # noqa: F401  (import 失败即测试失败)

    n = webapi._normalize_convert_to
    assert n("mp4") == ".mp4"
    assert n(".WEBM") == ".webm"
    assert n("jpeg") == ".jpeg"
    assert n(" JPG ") == ".jpg"
    assert n("zip") == ""
    assert n("") == ""
    assert n(None) == ""
