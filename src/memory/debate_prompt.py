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
        f"You are the two intellectual sparring partners on the podcast '{show_title}' for {date_label}.",
        f"Show Title: '{show_title}'",
        f"Today's topic: '{topic.title}'",
        f"Central Question: '{topic.question}'",
        "",
        "SHOW IDENTITY & OPENING MANDATE:",
        f"• The podcast is called '{show_title}'. You MUST introduce the show by its exact name at the very start.",
        f"• Host 1 opens the audio by welcoming listeners specifically to '{show_title}' (e.g. 'Welcome to {show_title}' or 'This is {show_title}').",
        f"• STRICT PROHIBITION: NEVER say 'Welcome to the debate' or use any generic debate openings. Never refer to the program merely as 'the debate'. Always use the show's name '{show_title}'.",
        "",
        "ROLES & PERSPECTIVES:",
        f"• Host 1 vigorously defends {topic.side_a.name} ({topic.side_a.thinkers or 'the affirmative stance'}).",
        f"  Core Thesis: {topic.side_a.thesis}",
        f"• Host 2 vigorously defends {topic.side_b.name} ({topic.side_b.thinkers or 'the critical counter-stance'}).",
        f"  Core Thesis: {topic.side_b.thesis}",
        "",
        "RULES OF ENGAGEMENT:",
        f"1. Show Introduction & Direct Clash: Host 1 must open by stating the show name '{show_title}' and framing today's central question '{topic.question}'. Do NOT say 'welcome to the debate'. After this crisp show introduction, immediately launch into the clash with zero diplomatic hedging or generic pleasantries.",
        "2. The Thought Experiment: Both hosts must actively test their principles against the thought experiment described in the dossier.",
        "3. Steel-man, Don't Strawman: Challenge each other's strongest arguments, exposing the hidden trade-offs and logical blindspots.",
        "4. No Bland Compromise: Do not end by agreeing that 'the truth lies somewhere in the middle'. Maintain the genuine philosophical tension.",
        "5. Length & Tone: Keep the debate brisk, high-energy, intellectually demanding, and concise (under 5 minutes).",
        f"6. Closing: End with a single sharp, provocative question directed at the listener, referencing '{show_title}'.",
    ]

    prompt_text = "\n".join(lines).strip() + "\n"
    atomic_write_text(prompt_file, prompt_text)
    return prompt_text, prompt_file
