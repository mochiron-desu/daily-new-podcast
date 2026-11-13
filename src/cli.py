"""Unified cross-platform CLI for Railway server, manual runs, and local failover."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import uvicorn

from .config import load_config
from .failover import create_app, probe_node, run_failover_check, sync_state_from_nodes
from .memory import StateDB
from .notifications import check_telegram_credentials
from .pipeline import run_daily_pipeline
from .processor import AudioMasteringError, resolve_ffmpeg_binary
from .producer import check_notebooklm_auth
from .publisher import build_publisher
from .utils import build_uvicorn_log_config, configure_logging, now_in_timezone


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


async def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(base_dir=Path(args.base_dir) if args.base_dir else None)
    db = StateDB(cfg.storage.db_path)
    pub = build_publisher(cfg, db)
    failures = 0

    print(f"Node ID      : {cfg.failover.node_id}")
    print(f"Node Role    : {cfg.failover.node_role} (priority={cfg.failover.node_priority})")
    print(f"Storage Dir  : {cfg.storage.root}")
    print(f"Timezone     : {cfg.schedule.timezone} (primary={cfg.schedule.primary_run_time}, cutoff=+{cfg.schedule.failover_cutoff_minutes}m)")

    try:
        ffmpeg_path = resolve_ffmpeg_binary(cfg.ffmpeg_bin)
        print(f"[OK] ffmpeg      : {ffmpeg_path}")
    except AudioMasteringError as exc:
        print(f"[FAIL] ffmpeg    : {exc}")
        failures += 1

    auth_res = await check_notebooklm_auth(
        cfg.notebooklm,
        storage_root=cfg.storage.root,
        prefer_writable_seed=(cfg.failover.node_role == "primary"),
        verify_live=args.live,
    )
    tag = "OK" if auth_res.ok else "FAIL"
    print(f"[{tag}] NotebookLM : {auth_res.mode} — {auth_res.detail}")
    if not auth_res.ok:
        failures += 1

    if args.live or cfg.publisher_backend == "local_rss":
        pub_ok, pub_msg = await pub.check_credentials()
        ptag = "OK" if pub_ok else "FAIL"
        print(f"[{ptag}] Publisher  : {cfg.publisher_backend} — {pub_msg}")
        if not pub_ok:
            failures += 1
    else:
        configured = bool(cfg.podbean.client_id and cfg.podbean.client_secret)
        ptag = "OK" if configured else "WARN"
        print(
            f"[{ptag}] Publisher  : podbean credentials "
            f"{'present' if configured else 'missing (run with --live after setting .env)'}"
        )

    tg_ok, tg_msg = await check_telegram_credentials(cfg.telegram, verify_live=args.live)
    tg_tag = "OK" if tg_ok else "WARN"
    print(f"[{tg_tag}] Telegram   : {tg_msg}")

    today = now_in_timezone(cfg.schedule.timezone).date()
    async with httpx.AsyncClient(timeout=cfg.schedule.http_timeout_seconds) as client:
        if cfg.failover.primary_url:
            st = await probe_node(client, cfg.failover.primary_url, today, cfg.failover.api_token)
            print(
                f"[{'OK' if st.reachable else 'WARN'}] Primary   : {st.url} "
                f"(reachable={st.reachable}, today_status={st.today_status or 'unknown'})"
            )
        for peer_url in cfg.failover.peer_urls:
            st = await probe_node(client, peer_url, today, cfg.failover.api_token)
            print(
                f"[{'OK' if st.reachable else 'WARN'}] Peer      : {st.url} "
                f"(reachable={st.reachable}, id={st.node_id}, priority={st.node_priority})"
            )

    return 1 if failures else 0


async def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(base_dir=Path(args.base_dir) if args.base_dir else None)
    db = StateDB(cfg.storage.db_path)
    today = now_in_timezone(cfg.schedule.timezone).date()
    arcs = db.get_developing_arcs()
    episodes = db.list_recent_episodes(limit=5)
    today_ep = db.get_episode_by_date(today)

    payload = {
        "node_id": cfg.failover.node_id,
        "node_role": cfg.failover.node_role,
        "today": today.isoformat(),
        "today_episode": today_ep.__dict__ if today_ep else None,
        "developing_arcs_count": len(arcs),
        "developing_arcs": [a.title for a in arcs[:10]],
        "recent_episodes": [
            {"date": e.episode_date, "status": e.status, "node": e.source_node, "title": e.title}
            for e in episodes
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily-news-podcast",
        description="Autonomous Daily News Podcast Pipeline (Railway primary + local failover)",
    )
    parser.add_argument("--base-dir", default=None, help="Repository root containing config.yaml")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING)")

    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run HTTP server + background daily/failover scheduler")
    serve_p.add_argument("--host", default=None, help="Override bind host")
    serve_p.add_argument("--port", type=int, default=None, help="Override bind port")
    serve_p.add_argument("--no-scheduler", action="store_true", help="Disable internal scheduler loop")

    run_p = sub.add_parser("run-once", help="Run the podcast pipeline immediately on this node")
    run_p.add_argument("--date", default=None, help="Target episode date (YYYY-MM-DD)")
    run_p.add_argument("--dry-run", action="store_true", help="Harvest + dossier + prompt only")
    run_p.add_argument("--skip-notebooklm", action="store_true", help="Skip NotebookLM & publish")
    run_p.add_argument("--force", action="store_true", help="Ignore existing published status")

    fail_p = sub.add_parser("failover-check", help="Check Railway/peers and run locally if down")
    fail_p.add_argument("--date", default=None, help="Target episode date (YYYY-MM-DD)")
    fail_p.add_argument("--force", action="store_true", help="Bypass time cutoff")
    fail_p.add_argument("--dry-run", action="store_true", help="Run failover without uploading")

    sub.add_parser("sync-state", help="Pull and merge state from Railway/peers into local SQLite")
    sub.add_parser("status", help="Show local state, today's episode, and active story arcs")

    doc_p = sub.add_parser("doctor", help="Check ffmpeg, NotebookLM auth, publisher, and peers")
    doc_p.add_argument("--live", action="store_true", help="Run live NotebookLM & Podbean checks")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    base_dir = Path(args.base_dir) if args.base_dir else None

    if args.command == "serve":
        cfg = load_config(base_dir=base_dir)
        app = create_app(cfg, enable_scheduler=not args.no_scheduler)
        uvicorn.run(
            app,
            host=args.host or cfg.failover.host,
            port=args.port or cfg.failover.port,
            log_level=args.log_level.lower(),
            log_config=build_uvicorn_log_config(args.log_level),
        )
        return 0

    if args.command == "run-once":
        cfg = load_config(base_dir=base_dir)
        res = asyncio.run(
            run_daily_pipeline(
                cfg,
                target_date=_parse_date(args.date),
                dry_run=args.dry_run,
                skip_notebooklm=args.skip_notebooklm,
                force=args.force,
            )
        )
        print(json.dumps(res.__dict__, indent=2))
        return 1 if res.status == "failed" else 0

    if args.command == "failover-check":
        cfg = load_config(base_dir=base_dir)
        outcome = asyncio.run(
            run_failover_check(
                cfg,
                target_date=_parse_date(args.date),
                force=args.force,
                dry_run=args.dry_run,
            )
        )
        out_dict = {
            "action": outcome.action,
            "reason": outcome.reason,
            "primary_status": outcome.primary_status.__dict__ if outcome.primary_status else None,
            "pipeline_result": outcome.pipeline_result.__dict__ if outcome.pipeline_result else None,
        }
        print(json.dumps(out_dict, indent=2, default=str))
        if outcome.pipeline_result and outcome.pipeline_result.status == "failed":
            return 1
        return 0

    if args.command == "sync-state":
        cfg = load_config(base_dir=base_dir)
        db = StateDB(cfg.storage.db_path)
        targets = [u for u in [cfg.failover.primary_url, *cfg.failover.peer_urls] if u]
        count = asyncio.run(
            sync_state_from_nodes(
                db,
                targets,
                api_token=cfg.failover.api_token,
                timeout_seconds=cfg.schedule.http_timeout_seconds,
            )
        )
        print(f"Synced state from {count} node(s).")
        return 0

    if args.command == "status":
        return asyncio.run(_cmd_status(args))

    if args.command == "doctor":
        return asyncio.run(_cmd_doctor(args))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

