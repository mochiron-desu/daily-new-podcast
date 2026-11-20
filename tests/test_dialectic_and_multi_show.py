"""Unit tests for multi-show configuration and debate topic engine."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import load_config
from src.harvester.debate_dossier import build_debate_dossier, render_debate_dossier_markdown
from src.harvester.topic_engine import (
    DebateTopic,
    SidePerspective,
    ensure_topic_bank_fresh,
    load_local_topics,
    save_topic_to_yaml,
    select_today_topic,
)
from src.memory.db import StateDB
from src.memory.debate_prompt import build_debate_prompt
from src.pipeline import run_daily_pipeline


def _make_dummy_topic(topic_id: str = "test-topic", category: str = "philosophy") -> DebateTopic:
    return DebateTopic(
        id=topic_id,
        title="Test Dilemma",
        category=category,
        question="Is test dilemma real?",
        side_a=SidePerspective(
            name="Proponents",
            thinkers="Thinker A",
            thesis="Pro thesis",
            arguments=("Arg A1", "Arg A2"),
            key_quote="Quote A",
        ),
        side_b=SidePerspective(
            name="Opponents",
            thinkers="Thinker B",
            thesis="Con thesis",
            arguments=("Arg B1", "Arg B2"),
            key_quote="Quote B",
        ),
        thought_experiment="Consider case X where Y collides with Z.",
        reading_excerpt="Context excerpt text.",
        source_urls=("https://example.com/source",),
        tags=("test",),
    )


def test_multi_show_config_isolation(tmp_path: Path) -> None:
    """Ensure different shows isolate storage roots and resolve distinct metadata."""
    default_cfg = load_config(base_dir=tmp_path)
    assert default_cfg.podcast.show_id == "daily_news"
    assert default_cfg.podcast.show_type == "news"

    dialectic_cfg = load_config(show="against_the_grain")
    assert dialectic_cfg.podcast.show_id == "against_the_grain"
    assert dialectic_cfg.podcast.show_title == "Against the Grain"
    assert dialectic_cfg.podcast.show_type == "debate"
    assert "against_the_grain" in str(dialectic_cfg.storage.root)
    assert dialectic_cfg.podcast.audio_format == "debate"
    assert dialectic_cfg.podcast.audio_length == "short"


def test_topic_selection_and_cooldown(tmp_path: Path) -> None:
    """Ensure topic selection respects aired cooldown and category order."""
    db_path = tmp_path / "state.sqlite"
    db = StateDB(db_path)

    t1 = _make_dummy_topic("topic-1", "philosophy")
    t2 = _make_dummy_topic("topic-2", "feminism")

    # Day 0 (Monday) targets philosophy
    picked = select_today_topic([t1, t2], db, target_date=date(2026, 11, 16))
    assert picked.id == "topic-1"

    # Air topic-1
    db.record_topic_aired(t1.id, t1.category, t1.title, date(2026, 11, 16))
    assert t1.id in db.get_recently_aired_topic_ids(days=30)

    # Now unplayed topic-2 is chosen
    picked_next = select_today_topic([t1, t2], db, target_date=date(2026, 11, 16))
    assert picked_next.id == "topic-2"


def test_debate_dossier_and_prompt_generation(tmp_path: Path) -> None:
    """Verify Markdown/PDF dossier and debate prompt synthesis."""
    topic = _make_dummy_topic("test-clash")
    dossiers_dir = tmp_path / "dossiers"
    prompts_dir = tmp_path / "prompts"

    artifacts = build_debate_dossier(
        topic=topic,
        episode_date=date(2026, 11, 19),
        show_title="The Daily Dialectic",
        dossiers_dir=dossiers_dir,
    )
    assert artifacts.markdown_path.is_file()
    assert artifacts.pdf_path.is_file()
    assert artifacts.pdf_path.stat().st_size > 500
    assert "Proponents" in artifacts.markdown_text
    assert "Opponents" in artifacts.markdown_text

    prompt_text, prompt_path = build_debate_prompt(
        episode_date=date(2026, 11, 19),
        show_title="The Daily Dialectic",
        topic=topic,
        prompts_dir=prompts_dir,
    )
    assert prompt_path.is_file()
    assert "RULES OF ENGAGEMENT" in prompt_text
    assert "Host 1 vigorously defends Proponents" in prompt_text


@pytest.mark.asyncio
async def test_dialectic_pipeline_dry_run() -> None:
    """Verify full end-to-end dry run for dialectic show."""
    cfg = load_config(show="dialectic")
    result = await run_daily_pipeline(
        cfg,
        target_date=date(2026, 11, 19),
        dry_run=True,
        force=True,
    )
    assert result.status == "dry_run_complete"
    assert result.title.endswith("— 2026-11-19")
    assert not result.title.startswith("Against the Grain")
    assert Path(result.dossier_pdf_path).is_file()
    assert Path(result.prompt_path).is_file()


def test_bruteforce_zero_repetition_and_programmatic_web_fetch(tmp_path: Path) -> None:
    """Bruteforce simulation: exhaust catalog, trigger internet fetch, and verify ZERO reuse."""
    db_path = tmp_path / "state.sqlite"
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir(parents=True)
    db = StateDB(db_path)

    # Start with only 2 seed topics
    t1 = _make_dummy_topic("seed-topic-1", "relationships")
    t2 = _make_dummy_topic("seed-topic-2", "philosophy")
    save_topic_to_yaml(t1, topics_dir)
    save_topic_to_yaml(t2, topics_dir)

    days_to_simulate = 8
    selected_topic_ids: list[str] = []
    selected_titles: list[str] = []

    for day_offset in range(days_to_simulate):
        current_date = date(2026, 12, 1) + timedelta(days=day_offset)

        # Auto-replenish if unplayed is low
        ensure_topic_bank_fresh(topics_dir, db, min_unplayed_threshold=2)

        local_topics = load_local_topics(topics_dir)
        picked = select_today_topic(local_topics, db, current_date, topics_dir=topics_dir)

        # STRICT ASSERTION: Never ever pick an already aired topic
        assert picked.id not in selected_topic_ids, (
            f"FATAL: Topic '{picked.id}' was reused on day {day_offset}! Re-airing is strictly forbidden."
        )

        selected_topic_ids.append(picked.id)
        selected_titles.append(picked.title)

        # Air the topic in DB
        db.record_topic_aired(picked.id, picked.category, picked.title, current_date)

    # 1. Total selected count must equal distinct count
    assert len(selected_topic_ids) == days_to_simulate
    assert len(set(selected_topic_ids)) == days_to_simulate

    # 2. Must have fetched new topics from the web because seed was only 2
    assert len(selected_topic_ids) > 2
    web_fetched_ids = [tid for tid in selected_topic_ids if not tid.startswith("seed-topic")]
    assert len(web_fetched_ids) >= 6, "Engine must programmatically harvest from web when local topics run out."

    # 3. Database must accurately track all aired topics
    aired_in_db = db.get_all_aired_topic_ids()
    for tid in selected_topic_ids:
        assert tid in aired_in_db
