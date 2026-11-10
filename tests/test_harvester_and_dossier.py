"""Tests for RSS parsing, clustering, and Markdown/PDF dossier creation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.config import FeedConfig, HarvesterConfig
from src.harvester import build_dossier, cluster_articles
from src.harvester.dossier_builder import MatchedArcContext
from src.harvester.rss_fetcher import parse_feed_xml

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Senate passes landmark AI safety bill in committee</title>
      <link>https://example.com/senate-ai-1</link>
      <description>&lt;p&gt;Lawmakers voted &amp;ldquo;yes&amp;rdquo; on Tuesday&#8212;advancing the bill.&lt;/p&gt;</description>
      <pubDate>Tue, 10 Nov 2026 04:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Open-source orbital telescope captures exoplanet atmosphere</title>
      <link>https://example.com/exoplanet</link>
      <description>Astronomers report clear spectral lines &#8377;200M budget.</description>
      <pubDate>Tue, 10 Nov 2026 03:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

SECOND_RSS = b"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
  <channel>
    <title>Second Outlet</title>
    <item>
      <title>Landmark AI safety bill clears Senate committee vote</title>
      <link>https://second.example.com/senate-ai-2</link>
      <description>Bipartisan coalition advances the Senate AI safety bill to floor debate.</description>
      <pubDate>Tue, 10 Nov 2026 04:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_parse_cluster_and_build_dossier(tmp_path: Path) -> None:
    cfg = HarvesterConfig()
    feed1 = FeedConfig(name="Outlet One", category="Tech", url="https://example.com/rss")
    feed2 = FeedConfig(name="Outlet Two", category="World", url="https://second.example.com/rss")

    art1 = parse_feed_xml(SAMPLE_RSS, feed1, max_items=10)
    art2 = parse_feed_xml(SECOND_RSS, feed2, max_items=10)
    assert len(art1) == 2
    assert len(art2) == 1

    now = datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)
    clusters = cluster_articles(art1 + art2, cfg, reference_now=now)
    # The two Senate AI articles should merge into one higher-scoring cluster
    assert len(clusters) == 2
    top = clusters[0]
    assert set(top.sources) == {"Outlet One", "Outlet Two"}

    matched = [
        MatchedArcContext(
            topic_slug="senate-ai-safety-bill",
            title="Senate AI Safety Bill",
            first_reported_date="2026-11-08",
            last_updated_date="2026-11-09",
            cumulative_summary="[2026-11-09] Draft introduced in subcommittee.",
            open_questions="Will it pass committee?",
            matched_cluster=top,
        )
    ]

    artifacts = build_dossier(
        episode_date=date(2026, 11, 10),
        show_title="Daily Morning Brief",
        clusters=clusters,
        matched_arcs=matched,
        dossiers_dir=tmp_path / "dossiers",
        cfg=cfg,
    )
    assert artifacts.markdown_path.is_file()
    assert artifacts.pdf_path.is_file()
    assert artifacts.pdf_path.read_bytes().startswith(b"%PDF")
    assert "Developing Story Arcs" in artifacts.markdown_text
    assert "Senate AI Safety Bill" in artifacts.markdown_text

