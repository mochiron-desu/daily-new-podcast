"""Pluggable podcast publisher protocol and shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class PublisherError(RuntimeError):
    """Raised when podcast host authentication, check, or upload fails."""


@dataclass(frozen=True)
class PublishedEpisode:
    remote_id: str
    title: str
    media_url: str
    permalink_url: str = ""


class PublisherBackend(ABC):
    """Abstract publisher interface for podcast hosting backends."""

    @abstractmethod
    async def check_credentials(self) -> tuple[bool, str]:
        """Verify publisher configuration and remote connectivity."""

    @abstractmethod
    async def has_episode_for_date(
        self,
        episode_date: date,
        expected_title: str,
    ) -> PublishedEpisode | None:
        """Return existing episode if already published for episode_date, else None."""

    @abstractmethod
    async def publish(
        self,
        *,
        audio_path: Path,
        title: str,
        description: str,
        episode_date: date,
    ) -> PublishedEpisode:
        """Upload audio and publish episode to the hosting backend."""

