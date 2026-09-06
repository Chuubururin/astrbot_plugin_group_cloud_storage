"""File operation constants — volume threshold and volume size.

Read via this module (attribute access at call time); tests patch this module
to change behavior.
"""

CHUNK_THRESHOLD_BYTES = 95 * 1024 * 1024
VOLUME_SIZE_BYTES = 90 * 1024 * 1024

__all__ = ["CHUNK_THRESHOLD_BYTES", "VOLUME_SIZE_BYTES"]
