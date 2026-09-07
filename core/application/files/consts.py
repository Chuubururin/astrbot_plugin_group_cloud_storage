"""File operation constants — volume threshold and volume size.

Read via this module (attribute access at call time); tests patch this module
to change behavior.  ``configure()`` lets the bootstrap layer override defaults
from the user config (e.g. ``volume_threshold_mb``).
"""

CHUNK_THRESHOLD_BYTES = 95 * 1024 * 1024
VOLUME_SIZE_BYTES = 90 * 1024 * 1024


def configure(cfg: dict | None = None) -> None:
    """Override constants from plugin config at startup."""
    global CHUNK_THRESHOLD_BYTES
    if cfg:
        mb = int(cfg.get("volume_threshold_mb", 95) or 95)
        mb = max(10, mb)  # enforce minimum to avoid degenerate thresholds
        CHUNK_THRESHOLD_BYTES = mb * 1024 * 1024


__all__ = ["CHUNK_THRESHOLD_BYTES", "VOLUME_SIZE_BYTES", "configure"]
