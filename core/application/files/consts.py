"""File operation constants — volume threshold and volume size.

Read via this module (attribute access at call time); tests patch this module
to change behavior.  ``configure()`` lets the bootstrap layer override defaults
from the user config (string-unit ``volume_threshold`` with the legacy
``volume_threshold_mb`` numeric key still supported).
"""

from core.config.model import PluginConfig
from core.units import format_size

CHUNK_THRESHOLD_BYTES = 95 * 1000 * 1000
VOLUME_SIZE_BYTES = 90 * 1024 * 1024


def configure(cfg) -> None:
    """Override constants from plugin config at startup.

    The volume threshold comes from the unified config resolution
    (``volume_threshold`` string unit, ``volume_threshold_mb`` legacy alias)
    and is floored at 10MB to avoid degenerate thresholds.
    """
    global CHUNK_THRESHOLD_BYTES
    if cfg:
        pc = cfg if isinstance(cfg, PluginConfig) else PluginConfig(cfg)
        threshold = pc.volume_threshold_bytes
        CHUNK_THRESHOLD_BYTES = max(threshold, 10 * 1000 * 1000)


def threshold_label() -> str:
    """Human-readable threshold (storage units, base 1000 — MB and up only)."""
    return format_size(CHUNK_THRESHOLD_BYTES)


__all__ = [
    "CHUNK_THRESHOLD_BYTES",
    "VOLUME_SIZE_BYTES",
    "configure",
    "threshold_label",
]
