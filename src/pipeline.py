"""End-to-end daily podcast pipeline orchestrator."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import AppConfig
from .harvester import build_dossier, harvest_feeds
from .harvester.debate_dossier import build_debate_dossier
from .harvester.topic_engine import (
    DebateTopic,
    ensure_topic_bank_fresh,
    fetch_1000wordphilosophy_topics,
    load_local_topics,
    select_today_topic,
)
from .memory import StateDB, build_continuity_prompt, match_developing_arcs
from .memory.debate_prompt import build_debate_prompt
from .notifications import TelegramNotifier
from .processor import master_episode_audio
from .producer import NotebookLMAuthError, generate_ephemeral_audio
from .publisher import PublisherBackend, build_publisher
from .utils import ensure_dir, now_in_timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    episode_date: str
    status: str  # published | skipped_already_published | skipped_locked | dry_run_complete | failed
    title: str
    node_id: str
    audio_path: str = ""
    remote_media_url: str = ""
    dossier_md_path: str = ""
    dossier_pdf_path: str = ""
    prompt_path: str = ""
    error_kind: str = ""
    error_message: str = ""


def prune_storage_artifacts(storage: Any, *, preserve_mp3: bool = False) -> int:
    """Remove historical dossiers, prompts, and optionally mp3 episodes to save volume disk."""
    deleted = 0
    dirs_to_clean = [
        getattr(storage, "dossiers_dir", None),
        getattr(storage, "prompts_dir", None),
    ]
    for folder in dirs_to_clean:
        if folder is not None and Path(folder).is_dir():
            for item in Path(folder).iterdir():
                if item.is_file():
                    item.unlink(missing_ok=True)
                    deleted += 1

    episodes_dir = getattr(storage, "episodes_dir", None)
    if not preserve_mp3 and episodes_dir is not None and Path(episodes_dir).is_dir():
        for item in Path(episodes_dir).iterdir():
            if item.is_file() and item.suffix in {".mp3", ".m4a"}:
                item.unlink(missing_ok=True)
                deleted += 1

    return deleted


def _build_episode_description(
    episode_date: date,
    show_title: str,
    clusters: list,
    matched_arcs: list,
) -> str:
    lines: list[str] = [
        f"{show_title} ({episode_date.strftime('%b %d, %Y')}):",
    ]
    seen_cats: set[str] = set()
    picked: list = []
    # Pick top story from distinct categories first to ensure balanced show notes
    for cluster in clusters:
        if cluster.category not in seen_cats:
            picked.append(cluster)
            seen_cats.add(cluster.category)
        if len(picked) >= 5:
            break
    if len(picked) < 4:
        for cluster in clusters:
            if cluster not in picked:
                picked.append(cluster)
            if len(picked) >= 4:
                break
    for cluster in picked:
        lines.append(f"• [{cluster.category}] {cluster.headline}")
    if matched_arcs and len("\n".join(lines)) < 380:
        lines.append(f"Updates: {', '.join(a.title[:45] for a in matched_arcs[:2])}")
    text = "\n".join(lines).strip()
    if len(text) > 495:
        text = text[:492].rstrip() + "..."
    return text


async def run_daily_pipeline(
    cfg: AppConfig,
    db: StateDB | None = None,
    publisher: PublisherBackend | None = None,
    notifier: TelegramNotifier | None = None,
    *,
    target_date: date | None = None,
    dry_run: bool = False,
    skip_notebooklm: bool = False,
    force: bool = False,
) -> PipelineResult:
    """Execute the daily news podcast pipeline idempotently for target_date."""
    ensure_dir(cfg.storage.root)
    state_db = db or StateDB(cfg.storage.db_path)
    pub = publisher or build_publisher(cfg, state_db)
    tg = notifier or TelegramNotifier(cfg.telegram)

    ep_date = target_date or now_in_timezone(cfg.schedule.timezone).date()
    date_str = ep_date.isoformat()
    episode_title = cfg.podcast.episode_title_template.format(
        date=date_str,
        topic=cfg.podcast.show_title,
    )
    node_id = cfg.failover.node_id

    async def _notify(stage: str, detail: str = "", *, status: str = "info") -> None:
        if dry_run:
            return
        await tg.send_stage(
            stage,
            detail,
            status=status,
            episode_date=ep_date,
            node_id=node_id,
            show_title=cfg.podcast.show_title,
        )

    if not force:
        local_ep = state_db.get_episode_by_date(ep_date)
        if local_ep is not None and local_ep.status == "published":
            logger.info("Episode for %s already marked published locally.", date_str)
            return PipelineResult(
                episode_date=date_str,
                status="skipped_already_published",
                title=local_ep.title,
                node_id=local_ep.source_node or node_id,
                audio_path=local_ep.audio_path,
                remote_media_url=local_ep.remote_media_url,
            )

        if not dry_run:
            try:
                existing_remote = await pub.has_episode_for_date(ep_date, episode_title)
            except Exception as exc:
                logger.warning("Remote duplicate check failed (continuing cautiously): %s", exc)
                existing_remote = None

            if existing_remote is not None:
                logger.info(
                    "Episode for %s already exists on remote publisher (%s). Syncing state.",
                    date_str,
                    existing_remote.title,
                )
                state_db.upsert_episode(
                    episode_date=ep_date,
                    title=existing_remote.title,
                    status="published",
                    source_node="remote-detected",
                    remote_media_url=existing_remote.media_url,
                )
                return PipelineResult(
                    episode_date=date_str,
                    status="skipped_already_published",
                    title=existing_remote.title,
                    node_id="remote-detected",
                    remote_media_url=existing_remote.media_url,
                )

    acquired, existing_lock = state_db.acquire_lock(
        ep_date,
        node_id=node_id,
        node_priority=cfg.failover.node_priority,
        ttl_seconds=cfg.schedule.lock_ttl_seconds,
    )
    if not acquired:
        logger.info(
            "Skipping run for %s: locked by '%s' until %s",
            date_str,
            existing_lock.owner_node_id,
            existing_lock.expires_at,
        )
        return PipelineResult(
            episode_date=date_str,
            status="skipped_locked",
            title=episode_title,
            node_id=existing_lock.owner_node_id,
        )

    raw_m4a_path = cfg.storage.episodes_dir / f"{date_str}_raw.m4a"
    final_mp3_path = cfg.storage.episodes_dir / f"{date_str}.mp3"

    try:
        if not dry_run:
            state_db.upsert_episode(
                episode_date=ep_date,
                title=episode_title,
                status="running",
                source_node=node_id,
            )
            await _notify(
                "Pipeline Started",
                f"Format: `{cfg.podcast.audio_format}` / `{cfg.podcast.audio_length}`\n"
                f"Feeds configured: {len(cfg.feeds)}",
                status="running",
            )

        # Ingestion & Dossier Compilation
        if cfg.podcast.show_type == "debate":
            topics_dir = cfg.podcast.topics_dir or (cfg.base_dir / "topics")
            # Auto-replenish from the web if unplayed topics run low
            try:
                ensure_topic_bank_fresh(topics_dir, state_db, min_unplayed_threshold=4)
            except Exception as refresh_exc:
                logger.warning("Topic auto-replenish warning: %s", refresh_exc)

            available_topics = load_local_topics(topics_dir)
            if not available_topics:
                available_topics = fetch_1000wordphilosophy_topics(limit=10)
            if not available_topics:
                raise RuntimeError("No debate topics could be loaded or fetched from topic bank.")

            topic = select_today_topic(
                available_topics,
                state_db,
                ep_date,
                topics_dir=topics_dir,
            )
            logger.info("Selected debate topic: '%s' (category=%s)", topic.title, topic.category)
            episode_title = cfg.podcast.episode_title_template.format(
                date=date_str,
                topic=topic.title,
            )

            dossier = build_debate_dossier(
                topic=topic,
                episode_date=ep_date,
                show_title=cfg.podcast.show_title,
                dossiers_dir=cfg.storage.dossiers_dir,
            )
            prompt_text, prompt_path = build_debate_prompt(
                episode_date=ep_date,
                show_title=cfg.podcast.show_title,
                topic=topic,
                prompts_dir=cfg.storage.prompts_dir,
                audio_format=cfg.podcast.audio_format,
                audio_length=cfg.podcast.audio_length,
            )
            desc_lines = [
                f"{cfg.podcast.show_title}: {topic.title} ({ep_date.strftime('%b %d, %Y')})",
                f"Question: {topic.question}",
                f"• {topic.side_a.name} ({topic.side_a.thinkers})",
                f"• {topic.side_b.name} ({topic.side_b.thinkers})",
            ]
            if topic.thought_experiment:
                desc_lines.append(f"Focus: {topic.thought_experiment[:140]}...")
            description = "\n".join(desc_lines).strip()
            clusters = []
            matched_arcs = []
        else:
            # 1. Harvest & cluster RSS news
            clusters = await harvest_feeds(cfg.feeds, cfg.harvester)

            # 2. Match against developing story arcs
            developing_arcs = state_db.get_developing_arcs()
            matched_arcs = match_developing_arcs(
                developing_arcs,
                clusters,
                threshold=cfg.harvester.arc_match_threshold,
            )

            # 3. Build Markdown + PDF dossier and Continuity Prompt
            dossier = build_dossier(
                episode_date=ep_date,
                show_title=cfg.podcast.show_title,
                clusters=clusters,
                matched_arcs=matched_arcs,
                dossiers_dir=cfg.storage.dossiers_dir,
                cfg=cfg.harvester,
            )
            prompt_text, prompt_path = build_continuity_prompt(
                episode_date=ep_date,
                show_title=cfg.podcast.show_title,
                matched_arcs=matched_arcs,
                clusters=clusters,
                prompts_dir=cfg.storage.prompts_dir,
                audio_format=cfg.podcast.audio_format,
                audio_length=cfg.podcast.audio_length,
            )
            description = _build_episode_description(
                ep_date, cfg.podcast.show_title, clusters, matched_arcs
            )

        if dry_run or skip_notebooklm:
            logger.info(
                "Dry run / skip-notebooklm complete for %s. Dossier=%s, Prompt=%s",
                date_str,
                dossier.pdf_path,
                prompt_path,
            )
            return PipelineResult(
                episode_date=date_str,
                status="dry_run_complete",
                title=episode_title,
                node_id=node_id,
                dossier_md_path=str(dossier.markdown_path),
                dossier_pdf_path=str(dossier.pdf_path),
                prompt_path=str(prompt_path),
            )

        if cfg.podcast.show_type == "debate":
            await _notify(
                "Debate Dossier Ready",
                f"Topic: *{topic.title}*\nCategory: `{topic.category}`\nQuestion: {topic.question}",
                status="info",
            )
        else:
            cat_counts = Counter(c.category for c in clusters)
            cat_summary = ", ".join(f"{cat}: {cnt}" for cat, cnt in cat_counts.items())
            await _notify(
                "News Harvested & Dossier Ready",
                f"Clusters: {len(clusters)} ({cat_summary})\n"
                f"Matched developing arcs: {len(matched_arcs)}",
                status="info",
            )

        # 4. Generate ephemeral NotebookLM audio overview (.m4a)
        await _notify(
            "Generating NotebookLM Audio",
            f"Uploading `{dossier.pdf_path.name}` and generating `{cfg.podcast.audio_format}` ({cfg.podcast.audio_length}) overview...",
            status="running",
        )
        await generate_ephemeral_audio(
            episode_date=ep_date,
            dossier_path=dossier.pdf_path,
            prompt_text=prompt_text,
            output_audio_path=raw_m4a_path,
            notebooklm_cfg=cfg.notebooklm,
            podcast_cfg=cfg.podcast,
            storage_root=cfg.storage.root,
            prefer_writable_seed=(cfg.failover.node_role == "primary"),
        )
        raw_kb = raw_m4a_path.stat().st_size // 1024
        await _notify(
            "NotebookLM Audio Downloaded",
            f"Raw audio downloaded (`{raw_kb} KB`). Starting ffmpeg mastering...",
            status="info",
        )

        # 5. Master broadcast MP3 via ffmpeg
        mastered_mp3 = master_episode_audio(
            raw_audio_path=raw_m4a_path,
            output_mp3_path=final_mp3_path,
            episode_title=episode_title,
            episode_date=ep_date,
            podcast_cfg=cfg.podcast,
            ffmpeg_bin=cfg.ffmpeg_bin,
        )
        raw_m4a_path.unlink(missing_ok=True)
        mp3_kb = mastered_mp3.stat().st_size // 1024
        await _notify(
            "Audio Mastered & Uploading",
            f"Mastered `{mastered_mp3.name}` ({mp3_kb} KB). Publishing to `{cfg.publisher_backend}`...",
            status="info",
        )

        # 6. Publish to Podbean (or configured backend)
        published = await pub.publish(
            audio_path=mastered_mp3,
            title=episode_title,
            description=description,
            episode_date=ep_date,
        )

        # 7. Persist episode & update continuity memory
        if cfg.podcast.show_type == "debate":
            state_db.upsert_episode(
                episode_date=ep_date,
                title=published.title,
                status="published",
                source_node=node_id,
                audio_path=str(mastered_mp3),
                remote_media_url=published.media_url,
                summary_text=description,
                topics_covered=[topic.title],
            )
            state_db.record_topic_aired(
                topic_id=topic.id,
                category=topic.category,
                title=topic.title,
                episode_date=ep_date,
                source_url=topic.source_urls[0] if topic.source_urls else "",
            )
            summary_preview = f"• [{topic.category}] {topic.title}\n• Q: {topic.question}"
        else:
            topics = [c.headline for c in clusters[:8]]
            state_db.upsert_episode(
                episode_date=ep_date,
                title=published.title,
                status="published",
                source_node=node_id,
                audio_path=str(mastered_mp3),
                remote_media_url=published.media_url,
                summary_text=description,
                topics_covered=topics,
            )
            state_db.update_story_arcs_after_episode(
                episode_date=ep_date,
                matched_arcs=matched_arcs,
                all_clusters=clusters,
                source_node=node_id,
                stale_arc_days=cfg.harvester.stale_arc_days,
            )
            summary_preview = "\n".join(f"• [{c.category}] {c.headline}" for c in clusters[:4])

        await _notify(
            "Episode Published",
            f"*{published.title}*\n"
            f"Media: {published.media_url or 'local'}\n\n"
            f"{summary_preview}",
            status="success",
        )

        # 8. Send generated artifacts via Telegram (if enabled)
        if cfg.telegram.send_artifacts and tg.enabled:
            logger.info("Sending podcast media and dossier artifacts via Telegram for %s", date_str)
            if mastered_mp3.is_file():
                await tg.send_audio(
                    mastered_mp3,
                    title=published.title,
                    performer=cfg.podcast.show_title,
                    caption=f"🎙️ *{published.title}*",
                )
            if cfg.telegram.send_pdf and dossier.pdf_path.is_file():
                await tg.send_document(
                    dossier.pdf_path,
                    caption=f"📄 {cfg.podcast.show_title} Dossier ({date_str})",
                )

        # 9. Clean up generated media/build artifacts to save volume storage
        if cfg.storage.cleanup_after_publish:
            logger.info("Cleaning up generated build artifacts for %s from storage", date_str)
            pruned_count = prune_storage_artifacts(
                cfg.storage,
                preserve_mp3=(cfg.publisher_backend == "local_rss"),
            )
            logger.info("Pruned %d storage artifacts", pruned_count)

        logger.info("Pipeline completed successfully for %s on node '%s'", date_str, node_id)
        return PipelineResult(
            episode_date=date_str,
            status="published",
            title=published.title,
            node_id=node_id,
            audio_path=str(mastered_mp3),
            remote_media_url=published.media_url,
            dossier_md_path=str(dossier.markdown_path),
            dossier_pdf_path=str(dossier.pdf_path),
            prompt_path=str(prompt_path),
        )

    except NotebookLMAuthError as exc:
        logger.error("NotebookLM auth failure on node '%s': %s", node_id, exc)
        if not dry_run:
            state_db.upsert_episode(
                episode_date=ep_date,
                title=episode_title,
                status="failed",
                source_node=node_id,
                error_kind="auth_error",
                error_message=str(exc),
            )
            await _notify(
                "Pipeline Failed (NotebookLM Auth)",
                f"Error: `{exc}`\nRefresh NotebookLM cookies or let local failover handle it.",
                status="failure",
            )
        return PipelineResult(
            episode_date=date_str,
            status="failed",
            title=episode_title,
            node_id=node_id,
            error_kind="auth_error",
            error_message=str(exc),
        )

    except Exception as exc:
        logger.exception("Pipeline failed on node '%s' for %s", node_id, date_str)
        error_kind = type(exc).__name__
        if not dry_run:
            state_db.upsert_episode(
                episode_date=ep_date,
                title=episode_title,
                status="failed",
                source_node=node_id,
                error_kind=error_kind,
                error_message=str(exc),
            )
            await _notify(
                f"Pipeline Failed ({error_kind})",
                f"Error: `{exc}`",
                status="failure",
            )
        return PipelineResult(
            episode_date=ep_date,
            status="failed",
            title=episode_title,
            node_id=node_id,
            error_kind=error_kind,
            error_message=str(exc),
        )

    finally:
        state_db.release_lock(ep_date, node_id)

