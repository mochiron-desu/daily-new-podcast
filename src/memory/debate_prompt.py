"""Debate continuity prompt synthesizer for NotebookLM Audio Overview."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..harvester.topic_engine import DebateTopic
from ..utils import atomic_write_text, ensure_dir


def build_debate_prompt(
    *,
    episode_date: date,
    show_title: str,
    topic: DebateTopic,
    prompts_dir: Path,
    audio_format: str = "debate",
    audio_length: str = "short",
) -> tuple[str, Path]:
    """Generate and persist NotebookLM debate instructions for the given topic."""
    ensure_dir(prompts_dir)
    date_label = episode_date.strftime("%B %d, %Y")
    prompt_file = prompts_dir / f"prompt_{episode_date.isoformat()}_{topic.id}.txt"

    lines: list[str] = [
        f"You are the two intellectual sparring partners on '{show_title}' for {date_label}.",
        f"Today's topic: '{topic.title}'",
        f"Central Question: '{topic.question}'",
        "",
        "ROLES & PERSPECTIVES:",
        f"• Host 1 vigorously defends {topic.side_a.name} ({topic.side_a.thinkers or 'the affirmative stance'}).",
        f"  Core Thesis: {topic.side_a.thesis}",
        f"• Host 2 vigorously defends {topic.side_b.name} ({topic.side_b.thinkers or 'the critical counter-stance'}).",
        f"  Core Thesis: {topic.side_b.thesis}",
        "",
        "RULES OF ENGAGEMENT:",
        "1. Direct Clash: Jump straight into the disagreement. Do not waste time with generic greetings or diplomatic hedging.",
        "2. The Thought Experiment: Both hosts must actively test their principles against the thought experiment described in the dossier.",
        "3. Steel-man, Don't Strawman: Challenge each other's strongest arguments, exposing the hidden trade-offs and logical blindspots.",
        "4. No Bland Compromise: Do not end by agreeing that 'the truth lies somewhere in the middle'. Maintain the genuine philosophical tension.",
        "5. Length & Tone: Keep the debate brisk, high-energy, intellectually demanding, and concise (under 5 minutes).",
        "6. Closing: End with a single sharp, provocative question directed at the listener.",
    ]

    prompt_text = "\n".join(lines).strip() + "\n"
    atomic_write_text(prompt_file, prompt_text)
    return prompt_text, prompt_file
