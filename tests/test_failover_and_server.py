"""Tests for Railway-to-local failover decisions, peer election, HTTP API, and pipeline."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import FailoverConfig, NotebookLMConfig, ScheduleConfig, load_config
from src.failover import (
    PeerStatus,
    create_app,
    elect_fallback_runner,
    evaluate_failover_need,
    run_failover_check,
)
from src.harvester.rss_fetcher import StoryCluster
from src.memory import StateDB
from src.pipeline import run_daily_pipeline
from src.producer import NotebookLMAuthError, resolve_notebooklm_storage
from src.publisher import LocalRssPublisher


def test_evaluate_failover_need_rules() -> None:
    sched = ScheduleConfig(timezone="Asia/Kolkata", primary_run_time="06:00", failover_cutoff_minutes=20)
    tz = ZoneInfo("Asia/Kolkata")

    # 1. Before cutoff with unreachable primary -> wait
    should, reason = evaluate_failover_need(
        primary_status=PeerStatus(url="https://rail", reachable=False),
        local_episode=None,
        now_local=datetime(2026, 11, 10, 6, 10, tzinfo=tz),
        schedule_cfg=sched,
    )
    assert should is False
    assert reason == "before_failover_cutoff"

    # 2. Past cutoff with unreachable primary -> failover!
    should, reason = evaluate_failover_need(
        primary_status=PeerStatus(url="https://rail", reachable=False),
        local_episode=None,
        now_local=datetime(2026, 11, 10, 6, 25, tzinfo=tz),
        schedule_cfg=sched,
    )
    assert should is True
    assert reason == "primary_unreachable_past_cutoff"

    # 3. Primary failed explicitly (e.g. NotebookLM cookies expired) -> immediate failover even at 06:02!
    should, reason = evaluate_failover_need(
        primary_status=PeerStatus(
            url="https://rail",
            reachable=True,
            today_status="failed",
            error_kind="auth_error",
        ),
        local_episode=None,
        now_local=datetime(2026, 11, 10, 6, 2, tzinfo=tz),
        schedule_cfg=sched,
    )
    assert should is True
    assert reason == "primary_failed:auth_error"

    # 4. Primary already published -> skip
    should, reason = evaluate_failover_need(
        primary_status=PeerStatus(url="https://rail", reachable=True, today_status="published"),
        local_episode=None,
        now_local=datetime(2026, 11, 10, 7, 0, tzinfo=tz),
        schedule_cfg=sched,
    )
    assert should is False
    assert reason == "primary_published"


def test_peer_election_priority_and_failure_skip() -> None:
    self_laptop = FailoverConfig(node_role="fallback", node_id="linux-laptop", node_priority=10)

    # Higher priority desktop (priority=5) online and healthy -> defer
    elected, reason, winner = elect_fallback_runner(
        self_laptop,
        [PeerStatus(url="http://desktop", reachable=True, node_id="win-desktop", node_role="fallback", node_priority=5)],
    )
    assert elected is False
    assert winner is not None
    assert winner.node_id == "win-desktop"
    assert "win-desktop" in reason

    # If win-desktop already failed today, linux-laptop takes over!
    elected, _, _ = elect_fallback_runner(
        self_laptop,
        [
            PeerStatus(
                url="http://desktop",
                reachable=True,
                node_id="win-desktop",
                node_role="fallback",
                node_priority=5,
                today_status="failed",
            )
        ],
    )
    assert elected is True


def test_fastapi_server_auth_and_state_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("API_TOKEN", "super-secret-token")
    monkeypatch.setenv("NODE_ROLE", "primary")
    monkeypatch.setenv("NODE_ID", "railway-primary")
    cfg = load_config(base_dir=Path(__file__).resolve().parent.parent)
    db = StateDB(cfg.storage.db_path)

    app = create_app(cfg, db=db, enable_scheduler=False)
    with TestClient(app) as client:
        # /health is unauthenticated for Railway probe
        h = client.get("/health")
        assert h.status_code == 200
        assert h.json()["node_id"] == "railway-primary"

        # /api/* requires token
        unauth = client.get("/api/state/export")
        assert unauth.status_code == 401

        headers = {"X-API-Token": "super-secret-token"}
        merge_res = client.post(
            "/api/state/merge",
            headers=headers,
            json={
                "story_arcs": [],
                "episodes": [
                    {
                        "episode_date": "2026-11-10",
                        "title": "Daily Brief — 2026-11-10",
                        "status": "published",
                        "source_node": "local-fallback",
                        "updated_at": "2026-11-10T01:00:00+00:00",
                    }
                ],
            },
        )
        assert merge_res.status_code == 200
        assert merge_res.json()["merged"]["episodes"] == 1

        exp = client.get("/api/state/export", headers=headers)
        assert exp.status_code == 200
        assert len(exp.json()["episodes"]) == 1

        # Path traversal attempt on /episodes
        bad = client.get("/episodes/secret.txt")
        assert bad.status_code == 400


async def test_pipeline_records_auth_error_for_failover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("NODE_ROLE", "primary")
    monkeypatch.setenv("NODE_ID", "railway-primary")
    monkeypatch.setenv("PUBLISHER_BACKEND", "local_rss")
    cfg = load_config(base_dir=Path(__file__).resolve().parent.parent)
    db = StateDB(cfg.storage.db_path)
    pub = LocalRssPublisher(cfg, db)

    async def fake_harvest(*args, **kwargs):
        return [
            StoryCluster(
                cluster_id="test-story",
                headline="Global summit reaches clean energy deal",
                category="World",
                sources=["BBC World"],
                urls=["https://example.com"],
                summaries=["Countries commit to new grid targets."],
                keywords={"global", "summit", "clean", "energy", "deal"},
                score=2.0,
                latest_published=None,
            )
        ]

    async def fake_auth_fail(*args, **kwargs):
        raise NotebookLMAuthError("Google session expired")

    import src.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "harvest_feeds", fake_harvest)
    monkeypatch.setattr(pipeline_mod, "generate_ephemeral_audio", fake_auth_fail)

    res = await run_daily_pipeline(cfg, db=db, publisher=pub, target_date=date(2026, 11, 10))
    assert res.status == "failed"
    assert res.error_kind == "auth_error"

    stored = db.get_episode_by_date(date(2026, 11, 10))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_kind == "auth_error"


def test_resolve_notebooklm_auth_b64_seeds_and_updates_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "")
    payload1 = json.dumps({"cookies": [{"name": "SID", "value": "v1"}]})
    payload2 = json.dumps({"cookies": [{"name": "SID", "value": "v2"}]})
    target = tmp_path / "storage" / "nb_state.json"

    cfg1 = NotebookLMConfig(
        storage_path=target,
        auth_b64=base64.b64encode(payload1.encode()).decode(),
    )
    resolved1 = resolve_notebooklm_storage(cfg1, tmp_path / "storage", prefer_writable_seed=True)
    assert resolved1 == target
    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["value"] == "v1"

    # Updating env secret refreshes seeded file automatically
    cfg2 = NotebookLMConfig(
        storage_path=target,
        auth_b64=base64.b64encode(payload2.encode()).decode(),
    )
    resolved2 = resolve_notebooklm_storage(cfg2, tmp_path / "storage", prefer_writable_seed=True)
    assert resolved2 == target
    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["value"] == "v2"


async def test_run_failover_check_executes_when_primary_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("NODE_ROLE", "fallback")
    monkeypatch.setenv("NODE_ID", "local-linux")
    monkeypatch.setenv("PRIMARY_URL", "https://railway.down.test")
    monkeypatch.setenv("PUBLISHER_BACKEND", "local_rss")
    cfg = load_config(base_dir=Path(__file__).resolve().parent.parent)
    db = StateDB(cfg.storage.db_path)
    pub = LocalRssPublisher(cfg, db)

    def down_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(down_handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=transport, *args, **kwargs),
    )

    import src.pipeline as pipeline_mod

    async def fake_harvest(*args, **kwargs):
        return [
            StoryCluster(
                cluster_id="c1",
                headline="Breaking tech standard approved",
                category="Tech",
                sources=["Hacker News"],
                urls=["https://example.com"],
                summaries=["Industry adopts open protocol."],
                keywords={"breaking", "tech", "standard", "approved"},
                score=2.0,
                latest_published=None,
            )
        ]

    monkeypatch.setattr(pipeline_mod, "harvest_feeds", fake_harvest)

    outcome = await run_failover_check(
        cfg,
        db=db,
        publisher=pub,
        target_date=date(2026, 11, 10),
        force=True,
        dry_run=True,
    )
    assert outcome.action == "failover_executed"
    assert outcome.primary_status is not None
    assert outcome.primary_status.reachable is False
    assert outcome.pipeline_result is not None
    assert outcome.pipeline_result.status == "dry_run_complete"


def test_cli_help_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli import main

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    assert main(["status"]) == 0


