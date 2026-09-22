"""精华文本 → PNG 渲染（纯 Pillow，自 DistributorService 抽出）。

画布尺寸按实际绘制行计算；字体跨平台择优（精华文本多为 CJK，CJK 字体排前）。
"""

from __future__ import annotations

from pathlib import Path

# Candidate TrueType paths for text->image rendering, tried in order
# (cross-platform; the essence text is usually CJK, so CJK-capable fonts come
# first). Falls back to Pillow's built-in bitmap font when none is present.
FONT_CANDIDATES = (
    # Linux (common distros)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)

DRAWN_LINE_CAP = 200


def load_render_font(ImageFont, size: int):
    """First available candidate TrueType font, else Pillow's default."""
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def render_text_to_image(text: str, dest: Path) -> Path:
    """Render ``text`` to a PNG saved at ``dest``. Pure Python + Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError(
            "Pillow (PIL) is required for rendering essence text to image. "
            "Install it with: pip install Pillow"
        ) from e
    lines = text.split("\n")
    font_size = 16
    line_height = font_size + 8
    padding = 20

    # BUG-17: CJK characters are full-width (~font_size per char) while
    # Latin characters are half-width (~font_size/2). Use a weighted
    # estimate: count CJK chars at 1.0x and others at 0.55x of font_size.
    import unicodedata

    def _line_pixel_width(line: str) -> int:
        w = 0
        for ch in line:
            if unicodedata.east_asian_width(ch) in ("W", "F"):
                w += font_size
            else:
                w += font_size * 55 // 100
        return w

    drawn = lines[:DRAWN_LINE_CAP]
    max_line_px = max((_line_pixel_width(line) for line in drawn), default=200)
    img_width = max(400, min(max_line_px + padding * 2, 1200))
    # Height follows the lines actually drawn: sizing it from len(lines)
    # allocated ~430MB for a 5000-line essence (1200x120040) and every
    # row past the cap was blank anyway (M17).
    img_height = max(100, len(drawn) * line_height + padding * 2)
    img = Image.new("RGB", (img_width, img_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = load_render_font(ImageFont, font_size)
    y = padding
    for line in drawn:
        draw.text((padding, y), line[:200], fill=(0, 0, 0), font=font)
        y += line_height
    img.save(dest.as_posix(), "PNG")
    return dest
