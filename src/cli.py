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
from .harvester.topic_engine import (
    ensure_topic_bank_fresh,
    fetch_1000wordphilosophy_topics,
    load_local_topics,
    save_topic_to_yaml,
    select_today_topic,
)
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
    common_p = argparse.ArgumentParser(add_help=False)
    common_p.add_argument("--base-dir", default=None, help="Repository root containing config.yaml")
    common_p.add_argument("--config", default=None, help="Explicit config YAML file path")
    common_p.add_argument("--show", default=None, help="Show identifier (e.g. 'daily_news', 'dialectic')")
    common_p.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING)")

    parser = argparse.ArgumentParser(
        prog="daily-news-podcast",
        description="Autonomous Daily News & Intellectual Debate Podcast Pipeline",
        parents=[common_p],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", parents=[common_p], help="Run HTTP server + background daily/failover scheduler")
    serve_p.add_argument("--host", default=None, help="Override bind host")
    serve_p.add_argument("--port", type=int, default=None, help="Override bind port")
    serve_p.add_argument("--no-scheduler", action="store_true", help="Disable internal scheduler loop")

    run_p = sub.add_parser("run-once", parents=[common_p], help="Run the podcast pipeline immediately on this node")
    run_p.add_argument("--date", default=None, help="Target episode date (YYYY-MM-DD)")
    run_p.add_argument("--dry-run", action="store_true", help="Harvest/select topic + dossier + prompt only")
    run_p.add_argument("--skip-notebooklm", action="store_true", help="Skip NotebookLM & publish")
    run_p.add_argument("--force", action="store_true", help="Ignore existing published status")

    fail_p = sub.add_parser("failover-check", parents=[common_p], help="Check Railway/peers and run locally if down")
    fail_p.add_argument("--date", default=None, help="Target episode date (YYYY-MM-DD)")
    fail_p.add_argument("--force", action="store_true", help="Bypass time cutoff")
    fail_p.add_argument("--dry-run", action="store_true", help="Run failover without uploading")

    sub.add_parser("sync-state", parents=[common_p], help="Pull and merge state from Railway/peers into local SQLite")
    sub.add_parser("status", parents=[common_p], help="Show local state, today's episode, and active story arcs")

    doc_p = sub.add_parser("doctor", parents=[common_p], help="Check ffmpeg, NotebookLM auth, publisher, and peers")
    doc_p.add_argument("--live", action="store_true", help="Run live NotebookLM & Podbean checks")

    tb_p = sub.add_parser("topic-bank", parents=[common_p], help="Manage and inspect the debate topic bank")
    tb_sub = tb_p.add_subparsers(dest="tb_action", required=True)

    tb_fetch = tb_sub.add_parser("fetch", help="Fetch fresh debate topics from 1000-Word Philosophy")
    tb_fetch.add_argument("--limit", type=int, default=10, help="Max topics to fetch")
    tb_fetch.add_argument("--save", action="store_true", help="Save fetched topics into topics/ directory")

    tb_sub.add_parser("list", help="List all local debate topics and their aired status")

    tb_sim = tb_sub.add_parser("simulate", help="Run multi-day simulation verifying zero reuse and web refresh")
    tb_sim.add_argument("--days", type=int, default=14, help="Number of days to simulate")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    base_dir = Path(args.base_dir) if args.base_dir else None
    config_path = Path(args.config) if args.config else None

    if args.command == "serve":
        cfg = load_config(base_dir=base_dir, config_path=config_path, show=args.show)
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
        cfg = load_config(base_dir=base_dir, config_path=config_path, show=args.show)
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
        cfg = load_config(base_dir=base_dir, config_path=config_path, show=args.show)
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
        cfg = load_config(base_dir=base_dir, config_path=config_path, show=args.show)
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

    if args.command == "topic-bank":
        cfg = load_config(base_dir=base_dir, config_path=config_path, show=args.show or "dialectic")
        topics_dir = cfg.podcast.topics_dir or (cfg.base_dir / "topics")

        if args.tb_action == "fetch":
            print(f"Fetching up to {args.limit} topics from 1000-Word Philosophy...")
            fetched = fetch_1000wordphilosophy_topics(limit=args.limit)
            print(f"Fetched {len(fetched)} topic(s):")
            for idx, t in enumerate(fetched, start=1):
                print(f"  {idx}. [{t.category}] {t.title}")
                if args.save:
                    saved_path = save_topic_to_yaml(t, topics_dir)
                    print(f"     -> Saved to {saved_path.relative_to(cfg.base_dir)}")
            return 0

        if args.tb_action == "list":
            local = load_local_topics(topics_dir)
            db = StateDB(cfg.storage.db_path)
            all_aired = db.get_all_aired_topic_ids()
            print(f"Loaded {len(local)} local topic(s) from {topics_dir}:")
            for idx, t in enumerate(local, start=1):
                status_tag = "AIRED" if t.id in all_aired else "READY"
                print(f"  {idx}. [{status_tag}] [{t.category}] {t.title} ({t.id})")
            return 0

        if args.tb_action == "simulate":
            from datetime import timedelta
            db = StateDB(cfg.storage.db_path)
            sim_days = args.days
            print(f"Starting {sim_days}-day bruteforce simulation for show '{cfg.podcast.show_id}'...")

            selected_ids: list[str] = []
            start_date = now_in_timezone(cfg.schedule.timezone).date()
            web_refreshes = 0

            for i in range(sim_days):
                cur_date = start_date + timedelta(days=i)
                added = ensure_topic_bank_fresh(topics_dir, db, min_unplayed_threshold=3)
                if added > 0:
                    web_refreshes += 1
                local = load_local_topics(topics_dir)
                picked = select_today_topic(local, db, cur_date, topics_dir=topics_dir)

                if picked.id in selected_ids:
                    print(f"❌ FATAL ERROR: Topic '{picked.id}' REPEATED on day {i+1} ({cur_date})!")
                    return 1
                selected_ids.append(picked.id)
                db.record_topic_aired(picked.id, picked.category, picked.title, cur_date)
                print(f"  Day {i+1:02d} ({cur_date}): [{picked.category}] {picked.title}")

            print("\n--- Simulation Summary ---")
            print(f"Days simulated      : {sim_days}")
            print(f"Distinct topics     : {len(selected_ids)}")
            print(f"Duplicates detected : 0 (STRICT ZERO REUSE VERIFIED)")
            print(f"Web refreshes fired : {web_refreshes}")
            return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

