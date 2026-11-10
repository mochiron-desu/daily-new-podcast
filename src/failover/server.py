"""FastAPI server for Railway primary scheduling, healthchecks, state sync, and local peers."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..config import AppConfig
from ..memory import StateDB
from ..pipeline import run_daily_pipeline
from ..publisher import build_publisher
from ..utils import now_in_timezone
from .coordinator import (
    notify_peers_of_primary_failure,
    push_state_to_nodes,
    run_failover_check,
    sync_state_from_nodes,
)

logger = logging.getLogger(__name__)


class LockRequest(BaseModel):
    run_date: str
    node_id: str
    node_priority: int = 10
    ttl_seconds: int = 2400


class ReleaseLockRequest(BaseModel):
    run_date: str
    node_id: str


class RunTriggerRequest(BaseModel):
    date: str | None = None
    dry_run: bool = False
    force: bool = False
    reason: str = "manual"


def _extract_token(request: Request) -> str:
    header_token = request.headers.get("X-API-Token")
    if header_token:
        return header_token.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _make_auth_dependency(cfg: AppConfig):
    expected = cfg.failover.api_token

    async def verify_api_auth(request: Request) -> None:
        if expected:
            provided = _extract_token(request)
            if not provided or not hmac.compare_digest(provided, expected):
                raise HTTPException(status_code=401, detail="Invalid or missing API token")
            return

        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(
                status_code=403,
                detail="API_TOKEN must be configured for non-loopback requests",
            )

    return verify_api_auth


async def _scheduler_loop(app: FastAPI, cfg: AppConfig, db: StateDB) -> None:
    """Background loop for primary daily run or standby fallback polling."""
    attempted_dates: set[str] = set()
    last_sync_ts = 0.0

    while True:
        try:
            now_local = now_in_timezone(cfg.schedule.timezone)
            today = now_local.date()
            today_str = today.isoformat()
            hour, minute = cfg.schedule.primary_hour_minute
            scheduled_dt = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if cfg.failover.node_role == "primary":
                if now_local >= scheduled_dt and today_str not in attempted_dates:
                    ep = db.get_episode_by_date(today)
                    if ep is None or ep.status not in {"published", "running"}:
                        attempted_dates.add(today_str)
                        logger.info("Scheduled primary run triggered for %s", today_str)
                        await sync_state_from_nodes(
                            db,
                            cfg.failover.peer_urls,
                            api_token=cfg.failover.api_token,
                            timeout_seconds=cfg.schedule.http_timeout_seconds,
                        )
                        result = await run_daily_pipeline(cfg, db=db, target_date=today)
                        if result.status == "published":
                            await push_state_to_nodes(
                                db,
                                cfg.failover.peer_urls,
                                api_token=cfg.failover.api_token,
                                timeout_seconds=cfg.schedule.http_timeout_seconds,
                            )
                        elif result.status == "failed":
                            await notify_peers_of_primary_failure(cfg, today)
                    else:
                        attempted_dates.add(today_str)
            else:
                # Standby local daemon: sync state every 15 minutes and run failover after cutoff.
                now_mono = time.monotonic()
                if now_mono - last_sync_ts >= 900:
                    last_sync_ts = now_mono
                    sync_targets = [
                        u for u in [cfg.failover.primary_url, *cfg.failover.peer_urls] if u
                    ]
                    await sync_state_from_nodes(
                        db,
                        sync_targets,
                        api_token=cfg.failover.api_token,
                        timeout_seconds=cfg.schedule.http_timeout_seconds,
                    )
                cutoff_dt = scheduled_dt + timedelta(minutes=cfg.schedule.failover_cutoff_minutes)
                if now_local >= cutoff_dt and today_str not in attempted_dates:
                    ep = db.get_episode_by_date(today)
                    if ep is None or ep.status not in {"published", "running"}:
                        outcome = await run_failover_check(cfg, db=db, target_date=today)
                        if outcome.action == "failover_executed":
                            attempted_dates.add(today_str)
                    else:
                        attempted_dates.add(today_str)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error inside scheduler loop")

        await asyncio.sleep(30)


def create_app(
    cfg: AppConfig,
    db: StateDB | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    """Build configured FastAPI application for Railway and peer nodes."""
    state_db = db or StateDB(cfg.storage.db_path)
    verify_auth = _make_auth_dependency(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = time.monotonic()
        app.state.running_task = None
        scheduler_task: asyncio.Task[None] | None = None
        if not cfg.failover.api_token:
            logger.warning("API_TOKEN is empty; non-loopback /api/* calls will be rejected.")
        if enable_scheduler:
            scheduler_task = asyncio.create_task(_scheduler_loop(app, cfg, state_db))
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
            active = getattr(app.state, "running_task", None)
            if active is not None and not active.done():
                active.cancel()
                with suppress(asyncio.CancelledError):
                    await active

    app = FastAPI(title="Daily News Podcast Node", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        today = now_in_timezone(cfg.schedule.timezone).date()
        ep = state_db.get_episode_by_date(today)
        started = getattr(app.state, "started_at", time.monotonic())
        return {
            "status": "ok",
            "node_id": cfg.failover.node_id,
            "node_role": cfg.failover.node_role,
            "node_priority": cfg.failover.node_priority,
            "today_date": today.isoformat(),
            "today_status": ep.status if ep else "none",
            "uptime_seconds": round(time.monotonic() - started, 1),
        }

    @app.get("/api/status/today", dependencies=[Depends(verify_auth)])
    async def status_today(
        date_str: str | None = Query(default=None, alias="date"),
    ) -> dict[str, Any]:
        target = (
            date.fromisoformat(date_str)
            if date_str
            else now_in_timezone(cfg.schedule.timezone).date()
        )
        ep = state_db.get_episode_by_date(target)
        lock = state_db.get_lock(target)
        return {
            "date": target.isoformat(),
            "node_id": cfg.failover.node_id,
            "node_role": cfg.failover.node_role,
            "node_priority": cfg.failover.node_priority,
            "episode": asdict(ep) if ep else None,
            "lock": asdict(lock) if lock else None,
        }

    @app.get("/api/state/export", dependencies=[Depends(verify_auth)])
    async def export_state() -> dict[str, Any]:
        return state_db.export_state()

    @app.post("/api/state/merge", dependencies=[Depends(verify_auth)])
    async def merge_state(payload: dict[str, Any]) -> dict[str, Any]:
        counts = state_db.merge_state(payload)
        return {"merged": counts}

    @app.post("/api/lock/acquire", dependencies=[Depends(verify_auth)])
    async def acquire_lock(req: LockRequest) -> dict[str, Any]:
        acquired, lock = state_db.acquire_lock(
            req.run_date,
            node_id=req.node_id,
            node_priority=req.node_priority,
            ttl_seconds=req.ttl_seconds,
        )
        return {"acquired": acquired, "lock": asdict(lock)}

    @app.post("/api/lock/release", dependencies=[Depends(verify_auth)])
    async def release_lock(req: ReleaseLockRequest) -> dict[str, Any]:
        released = state_db.release_lock(req.run_date, req.node_id)
        return {"released": released}

    @app.post("/api/run", status_code=202, dependencies=[Depends(verify_auth)])
    async def trigger_run(req: RunTriggerRequest) -> dict[str, Any]:
        active: asyncio.Task[Any] | None = getattr(app.state, "running_task", None)
        if active is not None and not active.done():
            raise HTTPException(status_code=409, detail="Pipeline task already running on this node")

        target = (
            date.fromisoformat(req.date)
            if req.date
            else now_in_timezone(cfg.schedule.timezone).date()
        )
        pub = build_publisher(cfg, state_db)

        async def _execute() -> None:
            result = await run_daily_pipeline(
                cfg,
                db=state_db,
                publisher=pub,
                target_date=target,
                dry_run=req.dry_run,
                force=req.force,
            )
            if result.status == "published" and not req.dry_run:
                targets = [u for u in [cfg.failover.primary_url, *cfg.failover.peer_urls] if u]
                await push_state_to_nodes(
                    state_db,
                    targets,
                    api_token=cfg.failover.api_token,
                    timeout_seconds=cfg.schedule.http_timeout_seconds,
                )

        app.state.running_task = asyncio.create_task(_execute())
        return {
            "accepted": True,
            "node_id": cfg.failover.node_id,
            "date": target.isoformat(),
            "reason": req.reason,
        }

    @app.get("/feed.xml")
    async def get_feed_xml() -> Response:
        if not cfg.storage.feed_xml_path.is_file():
            raise HTTPException(status_code=404, detail="Local RSS feed not generated yet")
        return Response(
            content=cfg.storage.feed_xml_path.read_bytes(),
            media_type="application/rss+xml; charset=utf-8",
        )

    @app.get("/episodes/{filename}")
    async def get_episode_audio(filename: str) -> FileResponse:
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.endswith(".mp3"):
            raise HTTPException(status_code=400, detail="Invalid episode filename")
        target = cfg.storage.episodes_dir / safe_name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Episode file not found")
        return FileResponse(target, media_type="audio/mpeg", filename=safe_name)

    return app

