"""Unified logging entry point.

Production uses the official AstrBot logging interface; test / standalone
environments (SDK not installed) automatically fall back to standard logging,
so core code stays unit-testable without the host.
"""

from __future__ import annotations

import logging

# Declared up front (annotation only -- inert at runtime under PEP 563) so that
# static analysis resolves `from core.log import logger` even when the AstrBot
# SDK is absent. Without it, pyright sees the try-branch import from a missing
# `astrbot.api` and reports the symbol as unknown at every consumer, which is a
# false positive in CI (requirements.txt intentionally omits host deps).
logger: logging.Logger

try:
    from astrbot.api import logger  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - standalone test environment without the SDK
    logger = logging.getLogger("group_cloud_storage")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
