"""Self-hosted podcast RSS 2.0 + iTunes XML feed builder & fallback publisher."""

from __future__ import annotations

import logging
import shutil
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from ..config import AppConfig
from ..memory.db import EpisodeRecord, StateDB
from ..utils import atomic_write_bytes, ensure_dir
from .base import PublishedEpisode, PublisherBackend, PublisherError

logger = logging.getLogger(__name__)

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", _ITUNES_NS)


def build_rss_feed_xml(cfg: AppConfig, episodes: list[EpisodeRecord]) -> bytes:
    """Build a Spotify-compliant RSS 2.0 + iTunes feed from published EpisodeRecords."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    base_url = cfg.failover.public_base_url.rstrip("/")
    ET.SubElement(channel, "title").text = cfg.podcast.show_title
    ET.SubElement(channel, "link").text = base_url
    ET.SubElement(channel, "description").text = cfg.podcast.show_description
    ET.SubElement(channel, "language").text = cfg.podcast.language
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}author").text = cfg.podcast.author
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}explicit").text = "false"

    for ep in episodes:
        if ep.status not in {"published", "generated"}:
            continue
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep.title
        ET.SubElement(item, "description").text = ep.summary_text or ep.title
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = (
            f"daily-news-podcast-{ep.episode_date}"
        )
        try:
            ep_dt = datetime.fromisoformat(f"{ep.episode_date}T06:00:00+00:00")
        except ValueError:
            ep_dt = datetime.now(timezone.utc)
        ET.SubElement(item, "pubDate").text = format_datetime(ep_dt)

        filename = Path(ep.audio_path).name if ep.audio_path else f"{ep.episode_date}.mp3"
        media_url = ep.remote_media_url or f"{base_url}/episodes/{filename}"
        file_len = "0"
        if ep.audio_path and Path(ep.audio_path).is_file():
            file_len = str(Path(ep.audio_path).stat().st_size)

        ET.SubElement(
            item,
            "enclosure",
            {"url": media_url, "length": file_len, "type": "audio/mpeg"},
        )

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


class LocalRssPublisher(PublisherBackend):
    """Stores MP3 locally and regenerates storage/feed.xml (useful for testing/self-hosting)."""

    def __init__(self, cfg: AppConfig, db: StateDB) -> None:
        self.cfg = cfg
        self.db = db

    async def check_credentials(self) -> tuple[bool, str]:
        return True, f"Local RSS backend ready (feed at {self.cfg.storage.feed_xml_path})"

    async def has_episode_for_date(
        self,
        episode_date: date,
        expected_title: str,
    ) -> PublishedEpisode | None:
        record = self.db.get_episode_by_date(episode_date)
        if record and record.status == "published":
            return PublishedEpisode(
                remote_id=record.episode_date,
                title=record.title,
                media_url=record.remote_media_url,
            )
        return None

    async def publish(
        self,
        *,
        audio_path: Path,
        title: str,
        description: str,
        episode_date: date,
    ) -> PublishedEpisode:
        if not audio_path.is_file():
            raise PublisherError(f"Audio file missing: {audio_path}")

        ensure_dir(self.cfg.storage.episodes_dir)
        dest_mp3 = self.cfg.storage.episodes_dir / f"{episode_date.isoformat()}.mp3"
        if audio_path.resolve() != dest_mp3.resolve():
            shutil.copy2(audio_path, dest_mp3)

        media_url = f"{self.cfg.failover.public_base_url.rstrip('/')}/episodes/{dest_mp3.name}"
        episodes = self.db.list_recent_episodes(limit=50)
        # Ensure current episode is included in feed generation before caller updates DB.
        synthesized = [
            ep for ep in episodes if ep.episode_date != episode_date.isoformat()
        ]
        synthesized.insert(
            0,
            EpisodeRecord(
                episode_date=episode_date.isoformat(),
                title=title,
                audio_path=str(dest_mp3),
                remote_media_url=media_url,
                status="published",
                summary_text=description,
                topics_covered_json="[]",
                error_message="",
                error_kind="",
                source_node=self.cfg.failover.node_id,
                updated_at="",
            ),
        )
        xml_bytes = build_rss_feed_xml(self.cfg, synthesized)
        atomic_write_bytes(self.cfg.storage.feed_xml_path, xml_bytes)
        logger.info("Updated local RSS feed at %s", self.cfg.storage.feed_xml_path)
        return PublishedEpisode(
            remote_id=episode_date.isoformat(),
            title=title,
            media_url=media_url,
            permalink_url=f"{self.cfg.failover.public_base_url.rstrip('/')}/feed.xml",
        )

