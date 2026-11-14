"""Markdown and PDF daily news dossier generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from fpdf import FPDF

from ..config import HarvesterConfig
from ..utils import atomic_write_bytes, atomic_write_text, ensure_dir
from .rss_fetcher import StoryCluster

_UNICODE_REPLACEMENTS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "--",
        "\u2026": "...",
        "\u2022": "*",
        "\u00a0": " ",
        "\u20ac": "EUR ",
        "\u00a3": "GBP ",
        "\u20b9": "INR ",
    }
)


@dataclass(frozen=True)
class MatchedArcContext:
    topic_slug: str
    title: str
    first_reported_date: str
    last_updated_date: str
    cumulative_summary: str
    open_questions: str
    matched_cluster: StoryCluster


@dataclass(frozen=True)
class DossierArtifacts:
    episode_date: date
    markdown_path: Path
    pdf_path: Path
    markdown_text: str


def _sanitize_latin1(text: str) -> str:
    normalized = text.translate(_UNICODE_REPLACEMENTS)
    return normalized.encode("latin-1", errors="replace").decode("latin-1")


def _format_cluster_block(idx: int, cluster: StoryCluster) -> list[str]:
    sources_str = ", ".join(cluster.sources)
    summary = cluster.merged_summary or "No additional summary available."
    lines = [
        f"### {idx}. {cluster.headline}",
        f"- **Category:** {cluster.category} | **Sources:** {sources_str}",
        f"- **Key Details:** {summary}",
    ]
    if cluster.urls:
        lines.append(f"- **Primary Link:** {cluster.urls[0]}")
    lines.append("")
    return lines


def render_dossier_markdown(
    episode_date: date,
    show_title: str,
    clusters: Sequence[StoryCluster],
    matched_arcs: Sequence[MatchedArcContext],
    cfg: HarvesterConfig,
) -> str:
    """Render structured daily news briefing dossier in Markdown."""
    lines: list[str] = [
        f"# {show_title} — Daily News Dossier ({episode_date.isoformat()})",
        "",
        f"Total curated story clusters: {len(clusters)}",
        "",
    ]

    if matched_arcs:
        lines.extend(["## Developing Story Arcs (Continuity From Previous Days)", ""])
        for arc in matched_arcs:
            lines.extend(
                [
                    f"### Arc: {arc.title} (Started {arc.first_reported_date}, Last covered {arc.last_updated_date})",
                    f"- **Previously Established:** {arc.cumulative_summary}",
                ]
            )
            if arc.open_questions:
                lines.append(f"- **Open Questions / Watchpoints:** {arc.open_questions}")
            lines.extend(
                [
                    f"- **Today's New Development:** {arc.matched_cluster.headline} — "
                    f"{arc.matched_cluster.merged_summary or 'New update reported.'}",
                    f"- **Sources Today:** {', '.join(arc.matched_cluster.sources)}",
                    "",
                ]
            )

    top_n = max(1, cfg.top_headlines_count)
    dev_n = max(0, cfg.developing_stories_count)

    top_clusters = list(clusters[:top_n])
    secondary_clusters = list(clusters[top_n : top_n + dev_n])
    rundown_clusters = list(clusters[top_n + dev_n :])

    lines.extend(["## Big Top Headlines", ""])
    for idx, cluster in enumerate(top_clusters, start=1):
        lines.extend(_format_cluster_block(idx, cluster))

    if secondary_clusters:
        lines.extend(["## Beat Briefs: National, Regional, International, Sports & Tech", ""])
        grouped: dict[str, list[StoryCluster]] = {}
        for cluster in secondary_clusters:
            grouped.setdefault(cluster.category, []).append(cluster)
        preferred_beats = ["National", "Regional", "International", "Sports", "Tech"]
        ordered_beats = [b for b in preferred_beats if b in grouped]
        ordered_beats.extend(b for b in grouped if b not in ordered_beats)
        item_num = 1
        for beat in ordered_beats:
            lines.append(f"### {beat}")
            for cluster in grouped[beat]:
                lines.extend(_format_cluster_block(item_num, cluster))
                item_num += 1

    if rundown_clusters:
        lines.extend(["## Quick-Hit Rundown", ""])
        for cluster in rundown_clusters:
            sources_str = ", ".join(cluster.sources)
            summary = cluster.merged_summary
            bullet = f"- **[{cluster.category}] {cluster.headline}** ({sources_str})"
            if summary:
                bullet += f": {summary}"
            lines.append(bullet)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_dossier_pdf_bytes(markdown_text: str, title: str) -> bytes:
    """Render Markdown dossier text into a clean multi-page PDF via fpdf2."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_title(_sanitize_latin1(title))
    pdf.set_margins(15, 15, 15)

    usable_width = pdf.w - pdf.l_margin - pdf.r_margin
    for raw_line in markdown_text.splitlines():
        line = _sanitize_latin1(raw_line.rstrip())
        if not line:
            pdf.ln(3)
            continue

        pdf.set_x(pdf.l_margin)
        if line.startswith("# "):
            pdf.set_font("Helvetica", style="B", size=16)
            pdf.multi_cell(usable_width, 8, line[2:].strip())
            pdf.ln(2)
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.set_font("Helvetica", style="B", size=13)
            pdf.multi_cell(usable_width, 7, line[3:].strip())
            pdf.ln(1)
        elif line.startswith("### "):
            pdf.ln(1)
            pdf.set_font("Helvetica", style="B", size=11)
            pdf.multi_cell(usable_width, 6, line[4:].strip())
        else:
            cleaned = line.replace("**", "")
            pdf.set_font("Helvetica", size=10)
            pdf.multi_cell(usable_width, 5.5, cleaned)

    output = pdf.output()
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    return str(output).encode("latin-1")


def build_dossier(
    episode_date: date,
    show_title: str,
    clusters: Sequence[StoryCluster],
    matched_arcs: Sequence[MatchedArcContext],
    dossiers_dir: Path,
    cfg: HarvesterConfig,
) -> DossierArtifacts:
    """Create both Markdown and PDF dossiers on disk atomically."""
    ensure_dir(dossiers_dir)
    date_str = episode_date.isoformat()
    md_path = dossiers_dir / f"dossier_{date_str}.md"
    pdf_path = dossiers_dir / f"dossier_{date_str}.pdf"

    md_text = render_dossier_markdown(
        episode_date=episode_date,
        show_title=show_title,
        clusters=clusters,
        matched_arcs=matched_arcs,
        cfg=cfg,
    )
    pdf_bytes = render_dossier_pdf_bytes(md_text, title=f"{show_title} - {date_str}")

    atomic_write_text(md_path, md_text)
    atomic_write_bytes(pdf_path, pdf_bytes)
    return DossierArtifacts(
        episode_date=episode_date,
        markdown_path=md_path,
        pdf_path=pdf_path,
        markdown_text=md_text,
    )

