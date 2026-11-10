"""Tests for RSS parsing, clustering, and Markdown/PDF dossier creation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.config import FeedConfig, HarvesterConfig
from src.harvester import build_dossier, cluster_articles
from src.harvester.dossier_builder import MatchedArcContext
from src.harvester.rss_fetcher import NewsArticle, parse_feed_xml

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


def test_cluster_articles_balances_all_four_categories() -> None:
    now = datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)
    articles: list[NewsArticle] = []
    # High-weight National articles would starve other categories without balancing.
    for idx in range(6):
        articles.append(
            NewsArticle(
                title=f"Parliament session update item {idx}",
                summary=f"National policy details {idx}.",
                url=f"https://example.com/nat/{idx}",
                source_name="National Daily",
                category="National",
                published_at=now,
                weight=2.0,
                keywords={f"nat{idx}", "parliament", "policy"},
            )
        )
    for cat in ("International", "Sports", "Tech"):
        for idx in range(3):
            articles.append(
                NewsArticle(
                    title=f"{cat} unique headline {idx}",
                    summary=f"{cat} summary {idx}.",
                    url=f"https://example.com/{cat.lower()}/{idx}",
                    source_name=f"{cat} Desk",
                    category=cat,
                    published_at=now,
                    weight=1.0,
                    keywords={cat.lower(), f"topic{idx}", f"item{idx}"},
                )
            )

    cfg = HarvesterConfig(max_clusters=8, cluster_similarity_threshold=0.8)
    clusters = cluster_articles(articles, cfg, reference_now=now)
    assert len(clusters) == 8
    cats = {c.category for c in clusters}
    assert cats == {"National", "International", "Sports", "Tech"}

