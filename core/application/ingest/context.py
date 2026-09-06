"""Shared dependencies and runtime settings for cloud ingestion."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class IngestContext:
    """Runtime context shared by the ingestion handlers."""

    api: Any
    store: Any
    queue: Any
    sync: Any
    tmp_dir: Path
    transfer: Any = None
    converter: Any = None
    essence_chunk_chars: int = 4500
    video_segment_seconds: int = 600
    fetch_max_bytes: int = 2 * 1024**3
    fetch_timeout: float = 180.0
    sync_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
