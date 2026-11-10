"""Failover coordinator: primary health probing, state sync, peer election, and fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

import httpx

from ..config import AppConfig, FailoverConfig, ScheduleConfig
from ..memory import EpisodeRecord, StateDB
from ..pipeline import PipelineResult, run_daily_pipeline
from ..publisher import PublisherBackend, build_publisher
from ..utils import now_in_timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerStatus:
    url: str
    reachable: bool
    node_id: str = ""
    node_role: str = ""
    node_priority: int = 999
    today_status: str = ""
    error_kind: str = ""
    lock_owner: str = ""


@dataclass(frozen=True)
class FailoverOutcome:
    action: str  # skipped | deferred_to_peer | failover_executed
    reason: str
    primary_status: PeerStatus | None = None
    pipeline_result: PipelineResult | None = None


def _auth_headers(api_token: str) -> dict[str, str]:
    if not api_token:
        return {}
    return {"X-API-Token": api_token}


async def probe_node(
    client: httpx.AsyncClient,
    base_url: str,
    run_date: date,
    api_token: str,
) -> PeerStatus:
    """Query /health and /api/status/today on a Railway or peer node."""
    cleaned_url = base_url.rstrip("/")
    try:
        health_res = await client.get(f"{cleaned_url}/health")
        if health_res.status_code >= 500:
            return PeerStatus(url=cleaned_url, reachable=False)
        health_res.raise_for_status()
        health = health_res.json()
    except Exception as exc:
        logger.debug("Node %s unreachable on /health: %s", cleaned_url, exc)
        return PeerStatus(url=cleaned_url, reachable=False)

    node_id = str(health.get("node_id") or "")
    node_role = str(health.get("node_role") or "")
    node_priority = int(health.get("node_priority") if health.get("node_priority") is not None else 999)
    today_status = str(health.get("today_status") or "")
    error_kind = ""
    lock_owner = ""

    try:
        status_res = await client.get(
            f"{cleaned_url}/api/status/today",
            params={"date": run_date.isoformat()},
            headers=_auth_headers(api_token),
        )
        if status_res.status_code == 200:
            data = status_res.json()
            ep = data.get("episode") or {}
            lock = data.get("lock") or {}
            today_status = str(ep.get("status") or today_status or "")
            error_kind = str(ep.get("error_kind") or "")
            lock_owner = str(lock.get("owner_node_id") or "")
    except Exception as exc:
        logger.debug("Could not fetch detailed /api/status/today from %s: %s", cleaned_url, exc)

    return PeerStatus(
        url=cleaned_url,
        reachable=True,
        node_id=node_id,
        node_role=node_role,
        node_priority=node_priority,
        today_status=today_status,
        error_kind=error_kind,
        lock_owner=lock_owner,
    )


async def sync_state_from_nodes(
    db: StateDB,
    urls: Sequence[str],
    api_token: str,
    timeout_seconds: float = 10.0,
) -> int:
    """Pull and merge state snapshots from reachable Railway/peer nodes."""
    merged_sources = 0
    unique_urls = [u.rstrip("/") for u in dict.fromkeys(urls) if u.strip()]
    if not unique_urls:
        return 0

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for url in unique_urls:
            try:
                res = await client.get(
                    f"{url}/api/state/export",
                    headers=_auth_headers(api_token),
                )
                if res.status_code == 200:
                    counts = db.merge_state(res.json())
                    merged_sources += 1
                    logger.info("Synced state from %s (%s)", url, counts)
            except Exception as exc:
                logger.debug("State pull from %s skipped: %s", url, exc)
    return merged_sources


async def push_state_to_nodes(
    db: StateDB,
    urls: Sequence[str],
    api_token: str,
    timeout_seconds: float = 10.0,
) -> int:
    """Push local state snapshot to reachable Railway/peer nodes."""
    pushed = 0
    unique_urls = [u.rstrip("/") for u in dict.fromkeys(urls) if u.strip()]
    if not unique_urls:
        return 0

    payload = db.export_state()
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for url in unique_urls:
            try:
                res = await client.post(
                    f"{url}/api/state/merge",
                    json=payload,
                    headers=_auth_headers(api_token),
                )
                if res.status_code == 200:
                    pushed += 1
                    logger.info("Pushed merged state to %s", url)
            except Exception as exc:
                logger.debug("State push to %s skipped: %s", url, exc)
    return pushed


def evaluate_failover_need(
    *,
    primary_status: PeerStatus | None,
    local_episode: EpisodeRecord | None,
    now_local: datetime,
    schedule_cfg: ScheduleConfig,
    force: bool = False,
) -> tuple[bool, str]:
    """Pure decision rule determining whether local fallback should activate.

    ``force=True`` bypasses the morning cutoff window when Railway is down or idle,
    but never overrides an already-published or actively-running episode.
    """
    if local_episode is not None and local_episode.status == "published":
        return False, "already_published_locally"

    if primary_status is not None and primary_status.reachable:
        if primary_status.today_status == "published":
            return False, "primary_published"
        if primary_status.today_status == "running":
            return False, "primary_running"
        if primary_status.lock_owner:
            return False, f"primary_locked_by:{primary_status.lock_owner}"
        if primary_status.today_status == "failed":
            detail = primary_status.error_kind or "error"
            return True, f"primary_failed:{detail}"

    if force:
        return True, "forced"

    tz = ZoneInfo(schedule_cfg.timezone)
    local_dt = now_local.astimezone(tz)
    hour, minute = schedule_cfg.primary_hour_minute
    scheduled_dt = local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    cutoff_dt = scheduled_dt + timedelta(minutes=schedule_cfg.failover_cutoff_minutes)

    if local_dt < cutoff_dt:
        return False, "before_failover_cutoff"

    if primary_status is None or not primary_status.reachable:
        return True, "primary_unreachable_past_cutoff"

    return True, "primary_missed_cutoff"


def elect_fallback_runner(
    self_cfg: FailoverConfig,
    peer_statuses: Sequence[PeerStatus],
) -> tuple[bool, str, PeerStatus | None]:
    """Deterministic election among reachable local fallback peers."""
    eligible_peers: list[PeerStatus] = []
    for peer in peer_statuses:
        if not peer.reachable or peer.node_role == "primary":
            continue
        if peer.today_status in {"published", "running"} or peer.lock_owner:
            return False, f"peer_active:{peer.node_id or peer.url}", peer
        if peer.today_status == "failed":
            # Skip peers that already failed today so another machine can try!
            continue
        eligible_peers.append(peer)

    if not eligible_peers:
        return True, "no_eligible_peers_self_elected", None

    self_key = (self_cfg.node_priority, self_cfg.node_id)
    best_peer = min(
        eligible_peers,
        key=lambda p: (p.node_priority, p.node_id or p.url),
    )
    peer_key = (best_peer.node_priority, best_peer.node_id or best_peer.url)

    if self_key <= peer_key:
        return True, "self_highest_priority", None
    return False, f"deferred_to_peer:{best_peer.node_id or best_peer.url}", best_peer


async def notify_peers_of_primary_failure(
    cfg: AppConfig,
    run_date: date,
) -> str | None:
    """When Railway primary fails (e.g. Google auth expired), trigger best online local peer."""
    if not cfg.failover.peer_urls:
        return None
    timeout = cfg.schedule.http_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        statuses = [
            await probe_node(client, url, run_date, cfg.failover.api_token)
            for url in cfg.failover.peer_urls
        ]
        candidates = [
            p for p in statuses if p.reachable and p.node_role != "primary" and p.today_status != "failed"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.node_priority, p.node_id or p.url))
        for candidate in candidates:
            try:
                res = await client.post(
                    f"{candidate.url}/api/run",
                    json={"date": run_date.isoformat(), "reason": "primary_failed"},
                    headers=_auth_headers(cfg.failover.api_token),
                )
                if res.status_code in (200, 202):
                    logger.info("Triggered failover run on peer %s", candidate.node_id or candidate.url)
                    return candidate.node_id or candidate.url
            except Exception as exc:
                logger.warning("Failed to trigger peer %s: %s", candidate.url, exc)
    return None


async def run_failover_check(
    cfg: AppConfig,
    db: StateDB | None = None,
    publisher: PublisherBackend | None = None,
    *,
    target_date: date | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> FailoverOutcome:
    """Check primary + peer nodes and execute the pipeline locally if Railway is down/failed."""
    state_db = db or StateDB(cfg.storage.db_path)
    pub = publisher or build_publisher(cfg, state_db)
    now_local = now_in_timezone(cfg.schedule.timezone)
    ep_date = target_date or now_local.date()

    # 1. Sync latest story_arcs & episode state from primary + sibling peers
    sync_urls: list[str] = []
    if cfg.failover.primary_url:
        sync_urls.append(cfg.failover.primary_url)
    sync_urls.extend(cfg.failover.peer_urls)
    await sync_state_from_nodes(
        state_db,
        sync_urls,
        api_token=cfg.failover.api_token,
        timeout_seconds=cfg.schedule.http_timeout_seconds,
    )

    # 2. Probe primary Railway deployment
    primary_status: PeerStatus | None = None
    peer_statuses: list[PeerStatus] = []
    async with httpx.AsyncClient(timeout=cfg.schedule.http_timeout_seconds) as client:
        if cfg.failover.primary_url:
            primary_status = await probe_node(
                client,
                cfg.failover.primary_url,
                ep_date,
                cfg.failover.api_token,
            )
        for peer_url in cfg.failover.peer_urls:
            peer_statuses.append(
                await probe_node(client, peer_url, ep_date, cfg.failover.api_token)
            )

    local_ep = state_db.get_episode_by_date(ep_date)
    should_failover, reason = evaluate_failover_need(
        primary_status=primary_status,
        local_episode=local_ep,
        now_local=now_local,
        schedule_cfg=cfg.schedule,
        force=force,
    )
    if not should_failover:
        logger.info("Failover check skipped (%s)", reason)
        return FailoverOutcome(
            action="skipped",
            reason=reason,
            primary_status=primary_status,
        )

    # 3. Multi-machine local peer election
    elected, election_reason, winning_peer = elect_fallback_runner(cfg.failover, peer_statuses)
    if not elected:
        if winning_peer is not None and winning_peer.today_status not in {"published", "running"}:
            # Politely ask higher-priority standby daemon to run if it hasn't started yet.
            async with httpx.AsyncClient(timeout=cfg.schedule.http_timeout_seconds) as client:
                try:
                    await client.post(
                        f"{winning_peer.url}/api/run",
                        json={"date": ep_date.isoformat(), "reason": reason},
                        headers=_auth_headers(cfg.failover.api_token),
                    )
                except Exception as exc:
                    logger.debug("Nudge to winning peer %s failed: %s", winning_peer.url, exc)
        logger.info("Deferring fallback (%s)", election_reason)
        return FailoverOutcome(
            action="deferred_to_peer",
            reason=election_reason,
            primary_status=primary_status,
        )

    # 4. Broadcast best-effort lock to reachable peers before running
    async with httpx.AsyncClient(timeout=cfg.schedule.http_timeout_seconds) as client:
        for peer in peer_statuses:
            if not peer.reachable:
                continue
            try:
                await client.post(
                    f"{peer.url}/api/lock/acquire",
                    json={
                        "run_date": ep_date.isoformat(),
                        "node_id": cfg.failover.node_id,
                        "node_priority": cfg.failover.node_priority,
                        "ttl_seconds": cfg.schedule.lock_ttl_seconds,
                    },
                    headers=_auth_headers(cfg.failover.api_token),
                )
            except Exception:
                pass

    logger.warning("Failover activated on '%s' (reason=%s)", cfg.failover.node_id, reason)
    result = await run_daily_pipeline(
        cfg,
        db=state_db,
        publisher=pub,
        target_date=ep_date,
        dry_run=dry_run,
        force=False,
    )

    # 5. Push updated state back to Railway (if up) and all reachable peers
    if not dry_run:
        await push_state_to_nodes(
            state_db,
            sync_urls,
            api_token=cfg.failover.api_token,
            timeout_seconds=cfg.schedule.http_timeout_seconds,
        )

    return FailoverOutcome(
        action="failover_executed",
        reason=reason,
        primary_status=primary_status,
        pipeline_result=result,
    )

