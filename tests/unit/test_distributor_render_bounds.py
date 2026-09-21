"""M17: 精华全文渲染的画布高度必须按实际绘制行数算。

绘制循环只画 lines[:200]，但画布高度按 len(lines) 算：5000 行的精华
全文会开出 1200x120040（约 430MB）的 RGB 画布，NUC 上直接 OOM；
而 200 行之后的部分本来就是白底。
"""

from __future__ import annotations

import pytest

from core.application.distributor import DistributorService

PILImage = pytest.importorskip("PIL.Image")

FONT_SIZE = 16
LINE_HEIGHT = FONT_SIZE + 8
PADDING = 20
DRAWN_LINE_CAP = 200


@pytest.mark.asyncio
async def test_render_canvas_height_follows_the_drawn_line_cap(tmp_path, monkeypatch):
    sizes: list[tuple[int, int]] = []
    real_new = PILImage.new

    def _tiny_new(mode, size, color=None):
        sizes.append(size)
        # 用缩小的真实图像代替：按 bug 的尺寸（1200x120040）真的会吃掉 430MB
        return real_new(mode, (min(size[0], 32), min(size[1], 32)), color)

    monkeypatch.setattr(PILImage, "new", _tiny_new)
    svc = DistributorService(store=None, api=None, tmp_dir=tmp_path)
    text = "\n".join(f"line {i}" for i in range(5000))

    out = await svc._render_text_to_image(text, "g1", 7)

    assert out.exists()
    assert len(sizes) == 1
    width, height = sizes[0]
    expected = DRAWN_LINE_CAP * LINE_HEIGHT + PADDING * 2
    assert height == expected, (
        f"画布高度 {height} 未按实际绘制行数计算（应为 {expected}）"
    )
    assert width <= 1200


@pytest.mark.asyncio
async def test_render_small_text_keeps_its_natural_height(tmp_path):
    """不触发上限时行为不变（行数少时高度仍随行数增长）。"""
    svc = DistributorService(store=None, api=None, tmp_dir=tmp_path)
    out = await svc._render_text_to_image("a\nb\nc", "g1", 8)
    assert out.exists()
    from PIL import Image

    with Image.open(out) as img:
        assert img.size[1] == 3 * LINE_HEIGHT + PADDING * 2
