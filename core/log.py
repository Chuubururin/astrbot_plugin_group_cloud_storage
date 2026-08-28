"""统一日志入口。

正式环境使用 AstrBot 官方日志接口（docs/06 §1 官方原则第 7 条）；
测试/独立运行环境（SDK 未装）自动回退到标准 logging，保证核心代码可脱离宿主单测。
"""

from __future__ import annotations

try:
    from astrbot.api import logger  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - SDK 未安装的独立测试环境
    import logging

    logger = logging.getLogger("group_cloud_storage")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
