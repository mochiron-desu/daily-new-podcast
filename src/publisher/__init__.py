"""Podcast publisher factory and backends."""

from __future__ import annotations

from ..config import AppConfig
from ..memory.db import StateDB
from .base import PublishedEpisode, PublisherBackend, PublisherError
from .podbean_client import PodbeanPublisher
from .rss_builder import LocalRssPublisher, build_rss_feed_xml


def build_publisher(cfg: AppConfig, db: StateDB) -> PublisherBackend:
    """Instantiate configured publisher backend ('podbean' or 'local_rss')."""
    if cfg.publisher_backend == "local_rss":
        return LocalRssPublisher(cfg, db)
    return PodbeanPublisher(cfg.podbean)


__all__ = [
    "LocalRssPublisher",
    "PodbeanPublisher",
    "PublishedEpisode",
    "PublisherBackend",
    "PublisherError",
    "build_publisher",
    "build_rss_feed_xml",
]

