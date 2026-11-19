"""Debate topic engine, local catalog loader, and 1000-Word Philosophy harvester."""

from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import yaml

from ..memory.db import StateDB
from ..utils import ensure_dir, slugify

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SidePerspective:
    name: str
    thinkers: str
    thesis: str
    arguments: tuple[str, ...] = ()
    key_quote: str = ""


@dataclass(frozen=True)
class DebateTopic:
    id: str
    title: str
    category: str
    question: str
    side_a: SidePerspective
    side_b: SidePerspective
    thought_experiment: str
    reading_excerpt: str = ""
    source_urls: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "question": self.question,
            "tags": list(self.tags),
            "source_urls": list(self.source_urls),
            "side_a": {
                "name": self.side_a.name,
                "thinkers": self.side_a.thinkers,
                "thesis": self.side_a.thesis,
                "arguments": list(self.side_a.arguments),
                "key_quote": self.side_a.key_quote,
            },
            "side_b": {
                "name": self.side_b.name,
                "thinkers": self.side_b.thinkers,
                "thesis": self.side_b.thesis,
                "arguments": list(self.side_b.arguments),
                "key_quote": self.side_b.key_quote,
            },
            "thought_experiment": self.thought_experiment,
            "reading_excerpt": self.reading_excerpt,
        }


def _clean_html(raw_html: str) -> str:
    """Strip HTML tags, scripts, and excessive whitespace cleanly."""
    text = re.sub(r"<style.*?</style>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    text = text.replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"')
    text = text.replace("&#8212;", " - ").replace("&#8211;", "-")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_topic_from_dict(data: dict) -> DebateTopic:
    """Build a DebateTopic instance from a dictionary."""
    sa = data.get("side_a", {}) or {}
    sb = data.get("side_b", {}) or {}

    side_a = SidePerspective(
        name=str(sa.get("name") or sa.get("label") or "Perspective A"),
        thinkers=str(sa.get("thinkers") or sa.get("champion") or ""),
        thesis=str(sa.get("thesis") or sa.get("core_thesis") or ""),
        arguments=tuple(str(a) for a in sa.get("arguments", [])),
        key_quote=str(sa.get("key_quote") or ""),
    )
    side_b = SidePerspective(
        name=str(sb.get("name") or sb.get("label") or "Perspective B"),
        thinkers=str(sb.get("thinkers") or sb.get("champion") or ""),
        thesis=str(sb.get("thesis") or sb.get("core_thesis") or ""),
        arguments=tuple(str(a) for a in sb.get("arguments", [])),
        key_quote=str(sb.get("key_quote") or ""),
    )

    topic_id = str(data.get("id") or slugify(str(data.get("title", "topic"))))
    title = str(data.get("title") or topic_id.replace("-", " ").title())
    category = str(data.get("category") or "philosophy")
    question = str(data.get("question") or title)
    thought_exp = str(data.get("thought_experiment") or "")
    excerpt = str(data.get("reading_excerpt") or "")
    sources = tuple(str(s.get("url") if isinstance(s, dict) else s) for s in data.get("source_urls", []) or data.get("sources", []))
    tags = tuple(str(t) for t in data.get("tags", []))

    return DebateTopic(
        id=topic_id,
        title=title,
        category=category,
        question=question,
        side_a=side_a,
        side_b=side_b,
        thought_experiment=thought_exp,
        reading_excerpt=excerpt,
        source_urls=sources,
        tags=tags,
    )


def load_topic_from_file(path: Path) -> DebateTopic:
    """Load and parse a YAML or Markdown debate topic file."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
            if not meta.get("reading_excerpt") and body:
                meta["reading_excerpt"] = body
            return load_topic_from_dict(meta)
    data = yaml.safe_load(text) or {}
    return load_topic_from_dict(data)


def load_local_topics(topics_dir: Path) -> list[DebateTopic]:
    """Scan topics_dir recursively for .yaml, .yml, or .md topic definitions."""
    if not topics_dir.is_dir():
        return []

    topics: list[DebateTopic] = []
    for ext in ("*.yaml", "*.yml", "*.md"):
        for path in topics_dir.rglob(ext):
            if path.name.startswith((".", "_")) or path.name.lower() == "readme.md":
                continue
            try:
                topic = load_topic_from_file(path)
                topics.append(topic)
            except Exception as exc:
                logger.warning("Failed to load topic from %s: %s", path, exc)
    return topics


def fetch_1000wordphilosophy_api(
    page: int = 1,
    per_page: int = 20,
    timeout: float = 15.0,
) -> list[DebateTopic]:
    """Fetch essays directly from 1000-Word Philosophy WordPress REST API."""
    import json

    url = f"https://1000wordphilosophy.com/wp-json/wp/v2/posts?per_page={per_page}&page={page}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (DailyNewsPodcast/2.0; +https://github.com/mochiron-desu)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            items = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("WordPress REST API fetch failed (page %d): %s", page, exc)
        return []

    topics: list[DebateTopic] = []
    for item in items:
        raw_title = item.get("title", {}).get("rendered", "")
        link = item.get("link", "")
        content_html = item.get("content", {}).get("rendered", "")
        if not raw_title or not link:
            continue
        clean_title = _clean_html(raw_title)
        clean_body = _clean_html(content_html)
        slug = str(item.get("slug") or slugify(clean_title))

        title_lower = clean_title.lower()
        if any(w in title_lower for w in ["love", "friendship", "family", "relationship"]):
            cat_label = "relationships"
        elif any(w in title_lower for w in ["ethic", "moral", "wrong", "right", "duty"]):
            cat_label = "ethics"
        elif any(w in title_lower for w in ["politi", "justice", "state", "law", "race", "rights"]):
            cat_label = "politics"
        elif any(w in title_lower for w in ["mind", "know", "science", "brain", "conscious", "epistem"]):
            cat_label = "cognitive_ethics"
        elif any(w in title_lower for w in ["gender", "femin"]):
            cat_label = "feminism"
        else:
            cat_label = "philosophy"

        question = clean_title if "?" in clean_title else f"How should we understand {clean_title}?"
        paragraphs = [p for p in clean_body.split("\n\n") if len(p.split()) > 15]
        excerpt = "\n\n".join(paragraphs[:4]) if paragraphs else clean_body[:1500]

        side_a = SidePerspective(
            name="The Foundational Argument",
            thinkers="Leading proponents in the essay",
            thesis=paragraphs[0] if paragraphs else "The conventional thesis.",
            arguments=("Primary conceptual argument outlined in the text.",),
        )
        side_b = SidePerspective(
            name="The Critical Rebuttal",
            thinkers="Critics and opposing traditions",
            thesis=paragraphs[1] if len(paragraphs) > 1 else "The opposing counter-thesis.",
            arguments=("The key objection or counter-example.",),
        )

        thought_exp = paragraphs[2] if len(paragraphs) > 2 else (
            f"Consider the central dilemma raised by '{clean_title}'. When the theoretical principles "
            f"are applied to a critical concrete case, which framework holds up?"
        )

        topic = DebateTopic(
            id=slug,
            title=clean_title,
            category=cat_label,
            question=question,
            side_a=side_a,
            side_b=side_b,
            thought_experiment=thought_exp,
            reading_excerpt=excerpt,
            source_urls=(link,),
            tags=(cat_label,),
        )
        topics.append(topic)

    return topics


def fetch_1000wordphilosophy_topics(
    limit: int = 15,
    timeout: float = 15.0,
) -> list[DebateTopic]:
    """Fetch essays from 1000-Word Philosophy (API preferred, RSS fallback)."""
    # 1. Attempt REST API first
    api_topics = fetch_1000wordphilosophy_api(page=1, per_page=limit, timeout=timeout)
    if api_topics:
        return api_topics

    # 2. Fallback to RSS feed
    feed_url = "https://1000wordphilosophy.com/feed/"
    req = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 (DailyNewsPodcast/2.0; +https://github.com/mochiron-desu)"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw_xml = response.read()
    except Exception as exc:
        logger.warning("Failed to fetch 1000-Word Philosophy feed: %s", exc)
        return []

    try:
        root = ET.fromstring(raw_xml)
    except Exception as exc:
        logger.warning("Failed to parse 1000-Word Philosophy XML: %s", exc)
        return []

    topics: list[DebateTopic] = []
    for item in root.findall(".//item")[:limit]:
        title_el = item.find("title")
        link_el = item.find("link")
        content_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")

        if title_el is None or link_el is None:
            continue

        raw_title = (title_el.text or "").strip()
        link = (link_el.text or "").strip()
        body_html = content_el.text if content_el is not None else ""
        clean_body = _clean_html(body_html)

        # Detect categories
        categories = [
            cat.text.strip()
            for cat in item.findall("category")
            if cat.text and not cat.text.strip().startswith("http")
        ]
        primary_cat = categories[0].lower() if categories else "philosophy"
        if "ethic" in primary_cat:
            cat_label = "ethics"
        elif "politi" in primary_cat or "social" in primary_cat:
            cat_label = "politics"
        elif "mind" in primary_cat or "science" in primary_cat or "epistem" in primary_cat:
            cat_label = "cognitive_ethics"
        elif "gender" in primary_cat or "femin" in primary_cat:
            cat_label = "feminism"
        else:
            cat_label = "philosophy"

        slug = slugify(raw_title)
        question = raw_title if "?" in raw_title else f"How should we understand {raw_title}?"

        # Extract first 300 words as excerpt and thought experiment hook
        paragraphs = [p for p in clean_body.split("\n\n") if len(p.split()) > 15]
        excerpt = "\n\n".join(paragraphs[:4]) if paragraphs else clean_body[:1500]

        side_a = SidePerspective(
            name="The Foundational Argument",
            thinkers="Leading proponents in the essay",
            thesis=paragraphs[0] if paragraphs else "The conventional or affirmative thesis.",
            arguments=("Primary conceptual argument outlined in the text.",),
        )
        side_b = SidePerspective(
            name="The Critical Rebuttal",
            thinkers="Critics and opposing philosophical traditions",
            thesis=paragraphs[1] if len(paragraphs) > 1 else "The opposing or skeptical counter-thesis.",
            arguments=("The key objection or paradox presented in the text.",),
        )

        thought_exp = paragraphs[2] if len(paragraphs) > 2 else (
            f"Consider the central dilemma raised by '{raw_title}'. When the theoretical principles "
            f"are applied to a critical concrete case, which framework holds up?"
        )

        topic = DebateTopic(
            id=slug,
            title=raw_title,
            category=cat_label,
            question=question,
            side_a=side_a,
            side_b=side_b,
            thought_experiment=thought_exp,
            reading_excerpt=excerpt,
            source_urls=(link,),
            tags=tuple(categories[:5]),
        )
        topics.append(topic)

    return topics


def save_topic_to_yaml(topic: DebateTopic, output_dir: Path) -> Path:
    """Save a DebateTopic into a YAML file inside output_dir."""
    cat_dir = output_dir / topic.category
    ensure_dir(cat_dir)
    file_path = cat_dir / f"{topic.id}.yaml"
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(topic.to_dict(), f, sort_keys=False, allow_unicode=True)
    return file_path


def ensure_topic_bank_fresh(
    topics_dir: Path,
    db: StateDB,
    min_unplayed_threshold: int = 4,
    fetch_limit: int = 15,
) -> int:
    """Check remaining unplayed topics, auto-fetching new ones from the web if low."""
    local_topics = load_local_topics(topics_dir)
    aired_ids = db.get_all_aired_topic_ids()
    unplayed = [t for t in local_topics if t.id not in aired_ids]

    if len(unplayed) >= min_unplayed_threshold:
        return 0

    logger.info(
        "Only %d unplayed topic(s) remain (threshold=%d). Programmatically fetching fresh topics from internet...",
        len(unplayed),
        min_unplayed_threshold,
    )
    existing_ids = {t.id for t in local_topics} | aired_ids
    added = 0

    # Page through the 1000-Word Philosophy archive until threshold is met
    for page in range(1, 8):
        batch = fetch_1000wordphilosophy_api(page=page, per_page=fetch_limit)
        if not batch:
            break
        for t in batch:
            if t.id not in existing_ids:
                save_topic_to_yaml(t, topics_dir)
                existing_ids.add(t.id)
                added += 1
                logger.info("Saved newly harvested topic: '%s' (%s)", t.title, t.id)
        if len(unplayed) + added >= min_unplayed_threshold:
            break

    if added == 0:
        rss_batch = fetch_1000wordphilosophy_topics(limit=fetch_limit)
        for t in rss_batch:
            if t.id not in existing_ids:
                save_topic_to_yaml(t, topics_dir)
                existing_ids.add(t.id)
                added += 1

    return added


def select_today_topic(
    topics: Sequence[DebateTopic],
    db: StateDB,
    target_date: date,
    category_preference: str | None = None,
    topics_dir: Path | None = None,
) -> DebateTopic:
    """Select an unplayed debate topic with category rotation. Strictly NEVER repeats aired topics."""
    aired_ids = db.get_all_aired_topic_ids()
    unplayed = [t for t in topics if t.id not in aired_ids]

    # If unplayed is empty, attempt emergency on-the-fly fetch from internet
    if not unplayed:
        logger.warning("All local topics have been aired! Attempting on-the-fly web harvest...")
        fetched = fetch_1000wordphilosophy_topics(limit=15)
        for t in fetched:
            if t.id not in aired_ids:
                if topics_dir:
                    save_topic_to_yaml(t, topics_dir)
                unplayed.append(t)

    if not unplayed:
        raise RuntimeError(
            "Zero unplayed topics remain in the catalog and no new topics could be fetched from the internet. "
            "Topics are strictly never repeated."
        )

    if category_preference:
        preferred = [t for t in unplayed if t.category.lower() == category_preference.lower()]
        if preferred:
            return preferred[0]

    # Category day-striping:
    # Monday=relationships, Tuesday=philosophy, Wednesday=feminism, Thursday=politics,
    # Friday=cognitive_ethics, Saturday/Sunday=ethics/rotation
    day_mod = target_date.weekday()
    category_order = ["relationships", "philosophy", "feminism", "politics", "cognitive_ethics", "ethics"]
    target_cat = category_order[day_mod % len(category_order)]

    matching_cat = [t for t in unplayed if t.category.lower() == target_cat]
    if matching_cat:
        return matching_cat[0]

    return unplayed[0]
