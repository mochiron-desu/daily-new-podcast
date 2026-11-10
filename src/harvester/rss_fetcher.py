"""Async RSS harvester with HTML sanitization and cross-outlet story clustering."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from calendar import timegm
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

import feedparser
import httpx

from ..config import FeedConfig, HarvesterConfig
from ..utils import jaccard_similarity, slugify, tokenize_keywords

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NewsArticle:
    title: str
    summary: str
    url: str
    source_name: str
    category: str
    published_at: datetime | None
    weight: float = 1.0
    keywords: frozenset[str] = field(default_factory=frozenset)


@dataclass
class StoryCluster:
    cluster_id: str
    headline: str
    category: str
    sources: list[str]
    urls: list[str]
    summaries: list[str]
    keywords: set[str]
    score: float
    latest_published: datetime | None

    @property
    def merged_summary(self) -> str:
        seen: list[str] = []
        for item in self.summaries:
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
            if len(seen) >= 3:
                break
        return " ".join(seen)


def clean_html_text(raw: str | None, max_chars: int = 600) -> str:
    """Strip HTML markup, unescape entities, and collapse whitespace."""
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    stripped = _TAG_RE.sub(" ", unescaped)
    collapsed = _WS_RE.sub(" ", stripped).strip()
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


def _parse_entry_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed is not None:
            try:
                ts = timegm(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def parse_feed_xml(
    xml_bytes: bytes,
    feed: FeedConfig,
    max_items: int,
) -> list[NewsArticle]:
    """Parse raw RSS/Atom bytes into normalized NewsArticle objects."""
    parsed = feedparser.parse(xml_bytes)
    articles: list[NewsArticle] = []
    for entry in parsed.entries[:max_items]:
        title = clean_html_text(entry.get("title"), max_chars=240)
        if not title:
            continue
        raw_summary = entry.get("summary") or entry.get("description") or ""
        summary = clean_html_text(raw_summary, max_chars=550)
        url = str(entry.get("link") or "").strip()
        published_at = _parse_entry_datetime(entry)
        keywords = frozenset(tokenize_keywords(f"{title} {summary[:200]}"))
        articles.append(
            NewsArticle(
                title=title,
                summary=summary,
                url=url,
                source_name=feed.name,
                category=feed.category,
                published_at=published_at,
                weight=feed.weight,
                keywords=keywords,
            )
        )
    return articles


async def _fetch_single_feed(
    client: httpx.AsyncClient,
    feed: FeedConfig,
    max_items: int,
) -> list[NewsArticle]:
    try:
        response = await client.get(feed.url, follow_redirects=True)
        response.raise_for_status()
        articles = parse_feed_xml(response.content, feed, max_items=max_items)
        logger.info("Fetched %d item(s) from %s", len(articles), feed.name)
        return articles
    except Exception as exc:
        logger.warning(
            "Failed to fetch feed '%s' (%s): %s: %s",
            feed.name,
            feed.url,
            type(exc).__name__,
            exc,
        )
        return []


def _recency_bonus(published_at: datetime | None, reference_now: datetime) -> float:
    if published_at is None:
        return 0.4
    age_hours = max(0.0, (reference_now - published_at).total_seconds() / 3600.0)
    if age_hours <= 6:
        return 1.5
    if age_hours <= 16:
        return 1.1
    if age_hours <= 24:
        return 0.8
    return 0.4


def _select_balanced_clusters(
    clusters: list[StoryCluster],
    max_clusters: int,
) -> list[StoryCluster]:
    """Pick up to max_clusters via round-robin across categories, then sort by score."""
    if max_clusters <= 0 or len(clusters) <= max_clusters:
        return clusters[:max_clusters]

    buckets: dict[str, list[StoryCluster]] = {}
    for cluster in clusters:
        buckets.setdefault(cluster.category, []).append(cluster)

    # Order categories consistently, keeping any unseen categories at the end.
    preferred_order = ["National", "International", "Sports", "Tech"]
    ordered_categories = [c for c in preferred_order if c in buckets]
    ordered_categories.extend(c for c in buckets if c not in ordered_categories)

    selected: list[StoryCluster] = []
    idx = 0
    while len(selected) < max_clusters:
        added_this_round = False
        for category in ordered_categories:
            bucket = buckets[category]
            if idx < len(bucket):
                selected.append(bucket[idx])
                added_this_round = True
                if len(selected) >= max_clusters:
                    break
        if not added_this_round:
            break
        idx += 1

    selected.sort(key=lambda c: (c.score, bool(c.merged_summary)), reverse=True)
    return selected


def cluster_articles(
    articles: Iterable[NewsArticle],
    cfg: HarvesterConfig,
    reference_now: datetime | None = None,
) -> list[StoryCluster]:
    """Filter recent articles, deduplicate/cluster similar stories, and rank clusters."""
    now = reference_now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.lookback_hours)

    all_articles = list(articles)
    recent = [a for a in all_articles if a.published_at is None or a.published_at >= cutoff]
    if len(recent) < cfg.min_articles_fallback:
        # Keep recent first, then fill from newest remaining items so sparse days still work.
        sorted_all = sorted(
            all_articles,
            key=lambda a: a.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        recent = sorted_all[: max(cfg.min_articles_fallback, len(recent))]

    # Deduplicate identical URLs or near-identical titles before clustering.
    seen_urls: set[str] = set()
    unique_articles: list[NewsArticle] = []
    for art in recent:
        if art.url and art.url in seen_urls:
            continue
        if art.url:
            seen_urls.add(art.url)
        unique_articles.append(art)

    clusters: list[StoryCluster] = []
    for art in unique_articles:
        art_tokens = set(art.keywords) or tokenize_keywords(art.title)
        best_cluster: StoryCluster | None = None
        best_sim = 0.0
        for cluster in clusters:
            sim = jaccard_similarity(art_tokens, cluster.keywords)
            if sim >= cfg.cluster_similarity_threshold and sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        bonus = _recency_bonus(art.published_at, now)
        if best_cluster is not None:
            if art.source_name not in best_cluster.sources:
                best_cluster.sources.append(art.source_name)
                best_cluster.score += 1.0 * art.weight
            if art.url and art.url not in best_cluster.urls:
                best_cluster.urls.append(art.url)
            if art.summary:
                best_cluster.summaries.append(art.summary)
            best_cluster.keywords |= art_tokens
            if art.published_at and (
                best_cluster.latest_published is None
                or art.published_at > best_cluster.latest_published
            ):
                best_cluster.latest_published = art.published_at
            best_cluster.score += 0.25 * bonus
        else:
            clusters.append(
                StoryCluster(
                    cluster_id=slugify(art.title),
                    headline=art.title,
                    category=art.category,
                    sources=[art.source_name],
                    urls=[art.url] if art.url else [],
                    summaries=[art.summary] if art.summary else [],
                    keywords=set(art_tokens),
                    score=(1.0 * art.weight) + bonus,
                    latest_published=art.published_at,
                )
            )

    clusters.sort(key=lambda c: (c.score, bool(c.merged_summary)), reverse=True)
    return _select_balanced_clusters(clusters, cfg.max_clusters)


async def harvest_feeds(
    feeds: Iterable[FeedConfig],
    cfg: HarvesterConfig,
    reference_now: datetime | None = None,
) -> list[StoryCluster]:
    """Fetch all configured RSS feeds concurrently and return ranked StoryClusters."""
    feed_list = list(feeds)
    if not feed_list:
        raise RuntimeError("No RSS feeds configured in config.yaml")

    headers = {
        "User-Agent": "DailyNewsPodcastBot/0.1 (+https://github.com/local/daily-news-podcast)"
    }
    timeout = httpx.Timeout(cfg.request_timeout_seconds)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        batches = await asyncio.gather(
            *[
                _fetch_single_feed(client, feed, max_items=cfg.max_items_per_feed)
                for feed in feed_list
            ]
        )

    articles = [art for batch in batches for art in batch]
    if not articles:
        raise RuntimeError("All configured RSS feeds failed or returned zero articles.")

    clusters = cluster_articles(articles, cfg, reference_now=reference_now)
    logger.info(
        "Harvested %d article(s) -> %d deduplicated story cluster(s)",
        len(articles),
        len(clusters),
    )
    return clusters

