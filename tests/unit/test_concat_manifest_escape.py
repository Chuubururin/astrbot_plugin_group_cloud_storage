"""回归：ffmpeg concat 清单必须转义单引号（清单注入）。

真实风险：段文件名源自 QQ 群文件名（合法含 `'`），ffmpeg 以 `-safe 0`
运行——未转义的引号可在 concat 清单里注入额外指令/任意本地文件路径，
把 bot 主机可读文件拼进产物外带。修复点：
composition/reassembler.py 的 concat_escape + files/download.py 的
视频重组清单写入。
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.composition import reassembler  # noqa: E402
from core.application.composition.reassembler import concat_escape  # noqa: E402


def test_concat_escape_exact_quoting():
    # ffmpeg 引号规则：关串 -> 转义引号 -> 重开
    assert concat_escape("a'b") == "'a'\\''b'"
    assert concat_escape("plain.mp4") == "'plain.mp4'"
    assert concat_escape(Path("/tmp/x'y.mp4")) == "'/tmp/x'\\''y.mp4'"


def test_concat_escape_neutralizes_injection():
    evil = "x.mp4'\nfile '/etc/passwd'\n#"
    quoted = concat_escape(evil)
    # 注入尝试后：清单行内不得残留裸换行（concat 解析器按行读取，
    # 引号内的换行同样会终止指令），且反解回来等于消毒后的原名
    assert "\n" not in quoted and "\r" not in quoted
    assert quoted.startswith("'") and quoted.endswith("'")
    assert quoted[1:-1].replace("'\\''", "'") == evil.replace("\n", "_")


@pytest.mark.asyncio
async def test_reassemble_video_manifest_is_escaped(tmp_path, monkeypatch):
    """reassemble_video 真实调用点：清单内容必须经过 concat_escape。"""
    monkeypatch.setattr(reassembler.shutil, "which", lambda _x: "/fake/ffmpeg")

    captured: dict[str, str] = {}

    def _fake_run(cmd, **kwargs):
        list_file = Path(cmd[cmd.index("-i") + 1])
        captured["manifest"] = list_file.read_text(encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(reassembler.subprocess, "run", _fake_run)

    seg = tmp_path / "seg'a.mp4"
    dest = tmp_path / "out.mp4"
    await reassembler.reassemble_video([seg], dest)

    manifest = captured["manifest"]
    assert "\n" in manifest
    line = manifest.splitlines()[0]
    assert line == f"file {concat_escape(seg)}", line
    assert "'\\''" in line, "单引号必须以 '\\'' 形态出现"


def test_download_video_manifest_uses_escape_helper():
    """files/download.py 的视频重组清单是第二处写入点，必须走同一 helper。

    该路径需要 ffmpeg + 完整服务环境（CI 的 contract 用例覆盖行为），
    这里用源码断言钉住"不得绕过 concat_escape 手写 f-string 引号"，
    防止未来改动把转义悄悄退回。
    """
    src = Path(__file__).resolve().parents[2] / "core" / "application" / "files" / "download.py"
    text = src.read_text(encoding="utf-8")
    assert "concat_escape" in text, "download.py 必须引用 concat_escape"
    # 违规形态：lf.write(f"file '{...}'") —— 引号直接包住插值、绕过 helper
    assert not re.search(r"""lf\.write\(\s*f?["']file\s*['"]\{""", text), (
        "download.py 存在绕过 concat_escape 的清单拼接"
    )
