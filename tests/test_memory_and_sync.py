"""Tests for SQLite continuity state, prompt synthesis, locks, and multi-node merge."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.harvester.rss_fetcher import StoryCluster
from src.memory import StateDB, build_continuity_prompt, match_developing_arcs


def test_story_arc_continuity_and_prompt(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.sqlite")
    day1_cluster = StoryCluster(
        cluster_id="senate-tech-bill",
        headline="Senate committee debates landmark tech antitrust bill",
        category="World",
        sources=["BBC World"],
        urls=["https://example.com/1"],
        summaries=["Committee hearings begin ahead of key Tuesday markup vote."],
        keywords={"senate", "committee", "tech", "antitrust", "bill"},
        score=2.5,
        latest_published=datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc),
    )

    db.update_story_arcs_after_episode(
        episode_date=date(2026, 11, 9),
        matched_arcs=[],
        all_clusters=[day1_cluster],
        source_node="railway-primary",
    )

    arcs = db.get_developing_arcs()
    assert len(arcs) == 1

    day2_cluster = StoryCluster(
        cluster_id="senate-tech-bill-passes",
        headline="Senate tech antitrust bill passes committee vote",
        category="World",
        sources=["NPR World"],
        urls=["https://example.com/2"],
        summaries=["Bill advances to full Senate floor vote next week."],
        keywords={"senate", "tech", "antitrust", "bill", "passes", "committee", "vote"},
        score=3.0,
        latest_published=datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc),
    )
    matched = match_developing_arcs(arcs, [day2_cluster], threshold=0.22)
    assert len(matched) == 1

    prompt_text, prompt_path = build_continuity_prompt(
        episode_date=date(2026, 11, 10),
        show_title="Daily Morning Brief",
        matched_arcs=matched,
        clusters=[day2_cluster],
        prompts_dir=tmp_path / "prompts",
        audio_format="brief",
        audio_length="short",
    )
    assert prompt_path.is_file()
    assert "CONTEXT & CONTINUITY FROM PREVIOUS EPISODES" in prompt_text
    assert "Picking up where we left off" in prompt_text
    assert "NO deep dives" in prompt_text
    assert "Indian National news, International affairs, Sports, and Tech" in prompt_text


def test_state_merge_prefers_published_and_newer_arcs(tmp_path: Path) -> None:
    railway_db = StateDB(tmp_path / "railway.sqlite")
    local_db = StateDB(tmp_path / "local.sqlite")

    # Railway failed on 2026-11-10
    railway_db.upsert_episode(
        episode_date=date(2026, 11, 10),
        title="Daily Brief — 2026-11-10",
        status="failed",
        source_node="railway-primary",
        error_kind="auth_error",
        error_message="Cookies expired",
    )
    # Local fallback succeeded and published
    local_db.upsert_episode(
        episode_date=date(2026, 11, 10),
        title="Daily Brief — 2026-11-10",
        status="published",
        source_node="suraj-linux",
        remote_media_url="https://podbean.example/ep1.mp3",
    )

    counts = railway_db.merge_state(local_db.export_state())
    assert counts["episodes"] == 1
    merged_ep = railway_db.get_episode_by_date(date(2026, 11, 10))
    assert merged_ep is not None
    assert merged_ep.status == "published"
    assert merged_ep.source_node == "suraj-linux"


def test_lock_mutual_exclusion_and_release(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "locks.sqlite")
    ok1, lock1 = db.acquire_lock("2026-11-10", node_id="node-a", node_priority=1, ttl_seconds=600)
    assert ok1 is True
    assert lock1.owner_node_id == "node-a"

    ok2, lock2 = db.acquire_lock("2026-11-10", node_id="node-b", node_priority=10, ttl_seconds=600)
    assert ok2 is False
    assert lock2.owner_node_id == "node-a"

    ok_same, _ = db.acquire_lock("2026-11-10", node_id="node-a", node_priority=1, ttl_seconds=600)
    assert ok_same is False

    assert db.release_lock("2026-11-10", "node-b") is False
    assert db.release_lock("2026-11-10", "node-a") is True

    ok3, _ = db.acquire_lock("2026-11-10", node_id="node-b", node_priority=10, ttl_seconds=600)
    assert ok3 is True

