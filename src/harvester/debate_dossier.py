"""Markdown and PDF debate dossier generator for philosophical/critical briefs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..utils import atomic_write_bytes, atomic_write_text, ensure_dir
from .dossier_builder import DossierArtifacts, render_dossier_pdf_bytes
from .topic_engine import DebateTopic


def render_debate_dossier_markdown(
    topic: DebateTopic,
    episode_date: date,
    show_title: str,
) -> str:
    """Render a structured, high-density debate briefing dossier in Markdown."""
    date_str = episode_date.strftime("%B %d, %Y")
    lines: list[str] = [
        f"# {show_title} Research Dossier",
        f"**Date:** {date_str} | **Category:** {topic.category.title()} | **Topic ID:** `{topic.id}`",
        "",
        f"## The Central Conflict: {topic.question}",
        "",
        "### Overview of the Clash",
        f"This briefing examines the clash surrounding **{topic.title}**.",
        "Both perspectives below represent rigorous philosophical, political, or ethical frameworks. "
        "Neither side is an easy strawman. The objective of this briefing is to contrast the core "
        "assumptions of each camp and explore how they respond to the central thought experiment.",
        "",
        f"## Perspective A: {topic.side_a.name}",
        f"- **Primary Thinkers / Tradition:** {topic.side_a.thinkers or 'Proponents'}",
        f"- **Core Thesis:** {topic.side_a.thesis}",
    ]

    if topic.side_a.arguments:
        lines.append("- **Key Arguments:**")
        for arg in topic.side_a.arguments:
            lines.append(f"  • {arg}")
    if topic.side_a.key_quote:
        lines.append(f'- **Anchor Quote/Concept:** "{topic.side_a.key_quote}"')

    lines.extend(
        [
            "",
            f"## Perspective B: {topic.side_b.name}",
            f"- **Primary Thinkers / Tradition:** {topic.side_b.thinkers or 'Critics'}",
            f"- **Core Thesis:** {topic.side_b.thesis}",
        ]
    )

    if topic.side_b.arguments:
        lines.append("- **Key Arguments:**")
        for arg in topic.side_b.arguments:
            lines.append(f"  • {arg}")
    if topic.side_b.key_quote:
        lines.append(f'- **Anchor Quote/Concept:** "{topic.side_b.key_quote}"')

    lines.extend(
        [
            "",
            "## The Decisive Thought Experiment & Practical Dilemma",
            topic.thought_experiment or "Consider the standard boundary condition where these principles collide in practice.",
            "",
            "## Foundational Reading & Context Excerpt",
            topic.reading_excerpt or "No extended excerpt provided.",
            "",
            "## Primary References",
        ]
    )

    if topic.source_urls:
        for url in topic.source_urls:
            lines.append(f"- {url}")
    else:
        lines.append("- Standalone curated topic catalog.")

    lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_debate_dossier(
    topic: DebateTopic,
    episode_date: date,
    show_title: str,
    dossiers_dir: Path,
) -> DossierArtifacts:
    """Create both Markdown and PDF debate dossiers on disk atomically."""
    ensure_dir(dossiers_dir)
    date_str = episode_date.isoformat()
    md_path = dossiers_dir / f"dossier_{date_str}_{topic.id}.md"
    pdf_path = dossiers_dir / f"dossier_{date_str}_{topic.id}.pdf"

    md_text = render_debate_dossier_markdown(
        topic=topic,
        episode_date=episode_date,
        show_title=show_title,
    )
    pdf_bytes = render_dossier_pdf_bytes(md_text, title=f"{show_title} - {topic.title}")

    atomic_write_text(md_path, md_text)
    atomic_write_bytes(pdf_path, pdf_bytes)

    return DossierArtifacts(
        episode_date=episode_date,
        markdown_path=md_path,
        pdf_path=pdf_path,
        markdown_text=md_text,
    )
