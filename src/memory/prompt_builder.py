"""Story arc matching and NotebookLM continuity prompt synthesis."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Sequence

from ..harvester.dossier_builder import MatchedArcContext
from ..harvester.rss_fetcher import StoryCluster
from ..utils import atomic_write_text, ensure_dir, jaccard_similarity, tokenize_keywords
from .db import StoryArcRecord


def match_developing_arcs(
    arcs: Sequence[StoryArcRecord],
    clusters: Sequence[StoryCluster],
    threshold: float = 0.22,
) -> list[MatchedArcContext]:
    """Match active developing story arcs against today's harvested clusters."""
    matched: list[MatchedArcContext] = []
    used_cluster_ids: set[str] = set()

    for arc in arcs:
        arc_tokens = tokenize_keywords(f"{arc.title} {arc.cumulative_summary}")
        best_cluster: StoryCluster | None = None
        best_sim = 0.0
        for cluster in clusters:
            if cluster.cluster_id in used_cluster_ids:
                continue
            sim = jaccard_similarity(arc_tokens, cluster.keywords)
            if sim >= threshold and sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        if best_cluster is not None:
            used_cluster_ids.add(best_cluster.cluster_id)
            matched.append(
                MatchedArcContext(
                    topic_slug=arc.topic_slug,
                    title=arc.title,
                    first_reported_date=arc.first_reported_date,
                    last_updated_date=arc.last_updated_date,
                    cumulative_summary=arc.cumulative_summary,
                    open_questions=arc.open_questions,
                    matched_cluster=best_cluster,
                )
            )
    return matched


def build_continuity_prompt(
    *,
    episode_date: date,
    show_title: str,
    matched_arcs: Sequence[MatchedArcContext],
    clusters: Sequence[StoryCluster],
    prompts_dir: Path,
    audio_format: str = "brief",
    audio_length: str = "short",
) -> tuple[str, Path]:
    """Generate and persist the NotebookLM Audio Overview instructions with continuity."""
    date_label = episode_date.strftime("%B %d, %Y")
    is_brief = audio_format.lower() == "brief" or audio_length.lower() == "short"

    if is_brief:
        opener = (
            f"You are delivering '{show_title}', a short, crisp, to-the-point Indian and global "
            f"morning news briefing for {date_label}."
        )
    else:
        opener = (
            f"You are hosting '{show_title}', a crisp, analytical morning news podcast for {date_label}."
        )

    lines: list[str] = [
        opener,
        "Open by briefly welcoming listeners to today's morning edition.",
        "",
    ]

    if matched_arcs:
        lines.append("CONTEXT & CONTINUITY FROM PREVIOUS EPISODES:")
        lines.append("Yesterday / recently we discussed these ongoing story arcs:")
        for arc in matched_arcs:
            last_line = arc.cumulative_summary.splitlines()[-1] if arc.cumulative_summary else ""
            lines.append(
                f"- Story Arc [{arc.title}] (last covered {arc.last_updated_date}): {last_line}"
            )
            lines.append(
                f"  -> Today's development: {arc.matched_cluster.headline}"
            )
        lines.append("")

    top_headlines = [c.headline for c in clusters[:4]]
    if top_headlines:
        lines.append("TODAY'S TOP HEADLINES TO ANCHOR THE BRIEFING:")
        for idx, headline in enumerate(top_headlines, start=1):
            lines.append(f"{idx}. {headline}")
        lines.append("")

    if is_brief:
        lines.extend(
            [
                "DELIVERY INSTRUCTIONS:",
                "1. Keep it short, crisp, and strictly to the point—NO deep dives, NO long banter, and NO filler.",
                "2. Cover all four beats cleanly in order: Indian National news, International affairs, Sports, and Tech.",
                "3. Give 1-2 punchy sentences per story: what happened and why it matters.",
                '4. For developing stories listed above, bridge in one quick phrase ("Picking up where we left off on [topic]...") before stating today\'s update.',
                "5. Do not mention PDFs, dossiers, RSS feeds, or source formatting—speak like a sharp morning news anchor.",
            ]
        )
    else:
        lines.extend(
            [
                "HOST INSTRUCTIONS:",
                "1. For developing stories listed above, seamlessly bridge from prior coverage: "
                '"Picking up where we left off on [topic]..." before explaining today\'s new update.',
                "2. For brand-new top stories, introduce them clearly as breaking developments and explain why they matter.",
                "3. Cover Indian National news, International news, Sports, and Tech in a cohesive flow.",
                "4. Keep the conversation natural, sharp, and listener-friendly; avoid reading bullet lists robotically.",
                "5. Do not mention PDFs, dossiers, RSS feeds, or source formatting—speak as seasoned news hosts.",
            ]
        )

    prompt_text = "\n".join(lines).strip() + "\n"
    ensure_dir(prompts_dir)
    prompt_path = prompts_dir / f"prompt_{episode_date.isoformat()}.txt"
    atomic_write_text(prompt_path, prompt_text)
    return prompt_text, prompt_path

