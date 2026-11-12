"""ffmpeg broadcast audio mastering: loudness normalization, intro chime, ID3 tags."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

from ..config import PodcastConfig
from ..utils import ensure_dir, subprocess_silent_kwargs

logger = logging.getLogger(__name__)


class AudioMasteringError(RuntimeError):
    """Raised when ffmpeg cannot be found or audio post-processing fails."""


def resolve_ffmpeg_binary(configured: str = "ffmpeg") -> str:
    """Resolve ffmpeg binary path cross-platform or raise AudioMasteringError."""
    candidate = Path(configured).expanduser()
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    raise AudioMasteringError(
        f"ffmpeg binary '{configured}' not found. Install ffmpeg or set FFMPEG_BIN."
    )


def build_ffmpeg_command(
    *,
    ffmpeg_path: str,
    raw_audio_path: Path,
    output_mp3_path: Path,
    episode_title: str,
    episode_date: date,
    podcast_cfg: PodcastConfig,
) -> list[str]:
    """Build cross-platform ffmpeg command for normalization, optional intro/outro jingles, and ID3 tags."""
    cmd: list[str] = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
    has_intro = bool(podcast_cfg.intro_audio_path and podcast_cfg.intro_audio_path.is_file())
    has_outro = bool(podcast_cfg.outro_audio_path and podcast_cfg.outro_audio_path.is_file())
    has_cover = bool(podcast_cfg.cover_art_path and podcast_cfg.cover_art_path.is_file())

    loudnorm_filter = f"loudnorm=I={podcast_cfg.target_lufs}:TP=-1.5:LRA=11"
    sr = podcast_cfg.sample_rate

    # Sequence of audio inputs: [intro?], raw_audio, [outro?]
    audio_inputs: list[Path] = []
    if has_intro:
        assert podcast_cfg.intro_audio_path is not None
        audio_inputs.append(podcast_cfg.intro_audio_path)
    audio_inputs.append(raw_audio_path)
    if has_outro:
        assert podcast_cfg.outro_audio_path is not None
        audio_inputs.append(podcast_cfg.outro_audio_path)

    for p in audio_inputs:
        cmd.extend(["-i", str(p)])

    cover_index: int | None = None
    if has_cover:
        assert podcast_cfg.cover_art_path is not None
        cover_index = len(audio_inputs)
        cmd.extend(["-i", str(podcast_cfg.cover_art_path)])

    if len(audio_inputs) > 1:
        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for i in range(len(audio_inputs)):
            filter_parts.append(
                f"[{i}:a]{loudnorm_filter},aresample={sr},aformat=channel_layouts=stereo[a{i}]"
            )
            concat_inputs.append(f"[a{i}]")
        filter_parts.append(f"{''.join(concat_inputs)}concat=n={len(audio_inputs)}:v=0:a=1[aout]")
        cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[aout]"])
    else:
        cmd.extend(["-af", loudnorm_filter, "-map", "0:a"])

    if cover_index is not None:
        cmd.extend(["-map", f"{cover_index}:v", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"])

    cmd.extend(
        [
            "-c:a",
            "libmp3lame",
            "-b:a",
            podcast_cfg.mp3_bitrate,
            "-ar",
            str(sr),
            "-id3v2_version",
            "3",
            "-metadata",
            f"title={episode_title}",
            "-metadata",
            f"artist={podcast_cfg.author}",
            "-metadata",
            f"album={podcast_cfg.show_title}",
            "-metadata",
            f"date={episode_date.isoformat()}",
            "-metadata",
            "genre=News",
            str(output_mp3_path),
        ]
    )
    return cmd


def master_episode_audio(
    *,
    raw_audio_path: Path,
    output_mp3_path: Path,
    episode_title: str,
    episode_date: date,
    podcast_cfg: PodcastConfig,
    ffmpeg_bin: str = "ffmpeg",
) -> Path:
    """Normalize loudness, optionally prepend intro jingle, encode MP3, and embed ID3 tags."""
    if not raw_audio_path.is_file():
        raise AudioMasteringError(f"Raw input audio not found: {raw_audio_path}")

    ffmpeg_path = resolve_ffmpeg_binary(ffmpeg_bin)
    ensure_dir(output_mp3_path.parent)
    tmp_mp3 = output_mp3_path.with_suffix(".tmp.mp3")
    tmp_mp3.unlink(missing_ok=True)

    cmd = build_ffmpeg_command(
        ffmpeg_path=ffmpeg_path,
        raw_audio_path=raw_audio_path,
        output_mp3_path=tmp_mp3,
        episode_title=episode_title,
        episode_date=episode_date,
        podcast_cfg=podcast_cfg,
    )
    logger.info("Mastering audio via ffmpeg -> %s", output_mp3_path.name)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        **subprocess_silent_kwargs(),
    )
    if proc.returncode != 0:
        tmp_mp3.unlink(missing_ok=True)
        raise AudioMasteringError(
            f"ffmpeg failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    if not tmp_mp3.is_file() or tmp_mp3.stat().st_size == 0:
        tmp_mp3.unlink(missing_ok=True)
        raise AudioMasteringError("ffmpeg produced an empty output file.")

    os.replace(tmp_mp3, output_mp3_path)
    logger.info(
        "Audio mastered: %s (%d KB)",
        output_mp3_path,
        output_mp3_path.stat().st_size // 1024,
    )
    return output_mp3_path

