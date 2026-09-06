"""Ingest domain - one-way collection import from the cloud drive into the
resource library (essence / video segmentation / album / URL fetch).

Boundary with bridge: bridge handles bidirectional transfer between group
files and OpenList; ingest turns cloud drive content into resource library
entries (metadata + volume/segment persistence).
"""
from .context import IngestContext
from .service import CloudIngestService
from .fetch import FetchMixin
from .video import VideoMixin
from .album import AlbumMixin
from .essence import EssenceMixin

__all__ = ["IngestContext", "CloudIngestService", "FetchMixin", "VideoMixin", "AlbumMixin", "EssenceMixin"]
