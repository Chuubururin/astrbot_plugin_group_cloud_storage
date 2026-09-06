"""Unified logging entry point.

Production uses the official AstrBot logging interface; test / standalone
environments (SDK not installed) automatically fall back to standard logging,
so core code stays unit-testable without the host.
"""

from __future__ import annotations

try:
    from astrbot.api import logger  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - standalone test environment without the SDK
    import logging

    logger = logging.getLogger("group_cloud_storage")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
