"""分发（distribute）纯逻辑件：与 IO / 服务装配解耦的可单元测试模块。

- ``media_spec``：QQ 相册媒体条目的名称匹配与规格择优（无状态）
- ``text_render``：精华文本 → PNG 渲染（Pillow，无状态）

服务编排（DistributorService 的 async 方法）留在
``core/application/distributor.py``；本包只承载其中的纯计算部分。
"""

from core.application.distribution.media_spec import (
    extract_media_url,
    pick_largest_spec,
    first_spec_url,
    select_media_entry,
    select_media_url,
)
from core.application.distribution.text_render import render_text_to_image

__all__ = [
    "extract_media_url",
    "pick_largest_spec",
    "first_spec_url",
    "select_media_entry",
    "select_media_url",
    "render_text_to_image",
]
