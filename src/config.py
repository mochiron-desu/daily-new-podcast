"""Typed configuration loader combining config.yaml and environment variables."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class FeedConfig:
    name: str
    category: str
    url: str
    weight: float = 1.0


@dataclass(frozen=True)
class PodcastConfig:
    show_title: str = "Daily Morning Brief"
    show_description: str = (
        "Autonomous two-host daily briefing covering global news, technology, and developing story arcs."
    )
    author: str = "Suraj & AI Hosts"
    language: str = "en"
    episode_title_template: str = "Daily Brief — {date}"
    audio_format: str = "deep-dive"
    audio_length: str = "default"
    mp3_bitrate: str = "192k"
    sample_rate: int = 44100
    target_lufs: float = -16.0
    intro_audio_path: Path | None = None
    cover_art_path: Path | None = None
    notebook_generation_timeout_seconds: int = 1800
    source_ready_timeout_seconds: int = 180
    delete_notebook_on_error: bool = True


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "Asia/Kolkata"
    primary_run_time: str = "06:00"
    failover_cutoff_minutes: int = 20
    lock_ttl_seconds: int = 2400
    http_timeout_seconds: float = 10.0

    @property
    def primary_hour_minute(self) -> tuple[int, int]:
        parts = self.primary_run_time.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid PRIMARY_RUN_TIME '{self.primary_run_time}', expected HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Out-of-range PRIMARY_RUN_TIME '{self.primary_run_time}'")
        return hour, minute


@dataclass(frozen=True)
class HarvesterConfig:
    lookback_hours: int = 36
    min_articles_fallback: int = 12
    max_items_per_feed: int = 12
    max_clusters: int = 18
    top_headlines_count: int = 3
    developing_stories_count: int = 6
    cluster_similarity_threshold: float = 0.28
    arc_match_threshold: float = 0.22
    stale_arc_days: int = 7
    request_timeout_seconds: float = 15.0


@dataclass(frozen=True)
class FailoverConfig:
    node_role: str = "fallback"  # "primary" | "fallback"
    node_id: str = "local-node"
    node_priority: int = 10
    api_token: str = ""
    host: str = "0.0.0.0"
    port: int = 8787
    public_base_url: str = "http://localhost:8787"
    primary_url: str = ""
    peer_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotebookLMConfig:
    profile: str = "default"
    storage_path: Path | None = None
    auth_json: str = ""
    auth_b64: str = ""


@dataclass(frozen=True)
class PodbeanConfig:
    client_id: str = ""
    client_secret: str = ""
    publish_status: str = "publish"
    episode_type: str = "public"
    api_base_url: str = "https://api.podbean.com/v1"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    source: str = "unconfigured"
    send_artifacts: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class StorageConfig:
    root: Path
    db_path: Path
    dossiers_dir: Path
    prompts_dir: Path
    episodes_dir: Path
    feed_xml_path: Path
    cleanup_after_publish: bool = True


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    storage: StorageConfig
    podcast: PodcastConfig
    schedule: ScheduleConfig
    harvester: HarvesterConfig
    failover: FailoverConfig
    notebooklm: NotebookLMConfig
    podbean: PodbeanConfig
    telegram: TelegramConfig
    publisher_backend: str
    ffmpeg_bin: str
    feeds: tuple[FeedConfig, ...] = field(default_factory=tuple)


def _resolve_optional_path(raw: str | None, base_dir: Path) -> Path | None:
    if not raw or not raw.strip():
        return None
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return candidate


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_url_list(raw: str) -> tuple[str, ...]:
    urls: list[str] = []
    for entry in raw.split(","):
        cleaned = entry.strip().rstrip("/")
        if cleaned:
            urls.append(cleaned)
    return tuple(urls)


def _resolve_telegram_config() -> TelegramConfig:
    """Resolve Telegram credentials from env/.env first, then telegram-notify config files."""
    token = _env_str("TELEGRAM_BOT_TOKEN", "")
    chat_id = _env_str("TELEGRAM_CHAT_ID", "")
    send_artifacts = _env_bool("TELEGRAM_SEND_ARTIFACTS", True)
    if token and chat_id:
        return TelegramConfig(
            bot_token=token,
            chat_id=chat_id,
            source="env",
            send_artifacts=send_artifacts,
        )

    candidates = (
        Path.home() / ".gemini" / "config" / "skills" / "telegram-notify" / "config.json",
        Path.home() / ".config" / "antigravity" / "telegram.json",
    )
    resolved_source = "env" if (token or chat_id) else "unconfigured"
    for cfg_path in candidates:
        if not cfg_path.is_file():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not token:
            token = str(data.get("bot_token") or data.get("token") or "").strip()
        if not chat_id:
            chat_id = str(data.get("chat_id") or "").strip()
        if token and chat_id:
            return TelegramConfig(
                bot_token=token,
                chat_id=chat_id,
                source=f"file:{cfg_path}",
                send_artifacts=send_artifacts,
            )

    return TelegramConfig(
        bot_token=token,
        chat_id=chat_id,
        source=resolved_source,
        send_artifacts=send_artifacts,
    )


def _load_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping in {config_path}")
    return loaded


def load_config(
    base_dir: Path | None = None,
    config_path: Path | None = None,
    env_file: Path | None = None,
) -> AppConfig:
    """Load application configuration from config.yaml and .env/environment variables."""
    resolved_base = (base_dir or Path(__file__).resolve().parent.parent).resolve()

    dotenv_path = env_file or (resolved_base / ".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    yaml_path = config_path or (resolved_base / "config.yaml")
    raw_cfg = _load_yaml(yaml_path)

    pod_raw = raw_cfg.get("podcast", {}) or {}
    sched_raw = raw_cfg.get("schedule", {}) or {}
    harv_raw = raw_cfg.get("harvester", {}) or {}
    feeds_raw = raw_cfg.get("feeds", []) or []

    raw_storage = _env_str("STORAGE_DIR", "./storage")
    storage_root = Path(raw_storage).expanduser()
    if not storage_root.is_absolute():
        storage_root = (resolved_base / storage_root).resolve()

    storage = StorageConfig(
        root=storage_root,
        db_path=storage_root / "state.sqlite",
        dossiers_dir=storage_root / "dossiers",
        prompts_dir=storage_root / "prompts",
        episodes_dir=storage_root / "episodes",
        feed_xml_path=storage_root / "feed.xml",
        cleanup_after_publish=_env_bool("STORAGE_CLEANUP_AFTER_PUBLISH", True),
    )

    podcast = PodcastConfig(
        show_title=str(pod_raw.get("show_title", PodcastConfig.show_title)),
        show_description=str(pod_raw.get("show_description", PodcastConfig.show_description)),
        author=str(pod_raw.get("author", PodcastConfig.author)),
        language=str(pod_raw.get("language", PodcastConfig.language)),
        episode_title_template=str(
            pod_raw.get("episode_title_template", PodcastConfig.episode_title_template)
        ),
        audio_format=str(pod_raw.get("audio_format", PodcastConfig.audio_format)),
        audio_length=str(pod_raw.get("audio_length", PodcastConfig.audio_length)),
        mp3_bitrate=str(pod_raw.get("mp3_bitrate", PodcastConfig.mp3_bitrate)),
        sample_rate=int(pod_raw.get("sample_rate", PodcastConfig.sample_rate)),
        target_lufs=float(pod_raw.get("target_lufs", PodcastConfig.target_lufs)),
        intro_audio_path=_resolve_optional_path(pod_raw.get("intro_audio_path"), resolved_base),
        cover_art_path=_resolve_optional_path(pod_raw.get("cover_art_path"), resolved_base),
        notebook_generation_timeout_seconds=int(
            pod_raw.get(
                "notebook_generation_timeout_seconds",
                PodcastConfig.notebook_generation_timeout_seconds,
            )
        ),
        source_ready_timeout_seconds=int(
            pod_raw.get("source_ready_timeout_seconds", PodcastConfig.source_ready_timeout_seconds)
        ),
        delete_notebook_on_error=bool(
            pod_raw.get("delete_notebook_on_error", PodcastConfig.delete_notebook_on_error)
        ),
    )

    schedule = ScheduleConfig(
        timezone=_env_str("TIMEZONE", str(sched_raw.get("timezone", ScheduleConfig.timezone))),
        primary_run_time=_env_str(
            "PRIMARY_RUN_TIME",
            str(sched_raw.get("primary_run_time", ScheduleConfig.primary_run_time)),
        ),
        failover_cutoff_minutes=_env_int(
            "FAILOVER_CUTOFF_MINUTES",
            int(sched_raw.get("failover_cutoff_minutes", ScheduleConfig.failover_cutoff_minutes)),
        ),
        lock_ttl_seconds=int(sched_raw.get("lock_ttl_seconds", ScheduleConfig.lock_ttl_seconds)),
        http_timeout_seconds=float(
            sched_raw.get("http_timeout_seconds", ScheduleConfig.http_timeout_seconds)
        ),
    )
    _ = schedule.primary_hour_minute  # Validate HH:MM early

    harvester = HarvesterConfig(
        lookback_hours=int(harv_raw.get("lookback_hours", HarvesterConfig.lookback_hours)),
        min_articles_fallback=int(
            harv_raw.get("min_articles_fallback", HarvesterConfig.min_articles_fallback)
        ),
        max_items_per_feed=int(
            harv_raw.get("max_items_per_feed", HarvesterConfig.max_items_per_feed)
        ),
        max_clusters=int(harv_raw.get("max_clusters", HarvesterConfig.max_clusters)),
        top_headlines_count=int(
            harv_raw.get("top_headlines_count", HarvesterConfig.top_headlines_count)
        ),
        developing_stories_count=int(
            harv_raw.get("developing_stories_count", HarvesterConfig.developing_stories_count)
        ),
        cluster_similarity_threshold=float(
            harv_raw.get(
                "cluster_similarity_threshold", HarvesterConfig.cluster_similarity_threshold
            )
        ),
        arc_match_threshold=float(
            harv_raw.get("arc_match_threshold", HarvesterConfig.arc_match_threshold)
        ),
        stale_arc_days=int(harv_raw.get("stale_arc_days", HarvesterConfig.stale_arc_days)),
        request_timeout_seconds=float(
            harv_raw.get("request_timeout_seconds", HarvesterConfig.request_timeout_seconds)
        ),
    )

    node_role = _env_str("NODE_ROLE", "fallback").lower()
    if node_role not in {"primary", "fallback"}:
        raise ValueError(f"NODE_ROLE must be 'primary' or 'fallback', got '{node_role}'")

    default_node_id = "railway-primary" if node_role == "primary" else socket.gethostname()
    node_id = _env_str("NODE_ID", default_node_id) or default_node_id

    failover = FailoverConfig(
        node_role=node_role,
        node_id=node_id,
        node_priority=_env_int("NODE_PRIORITY", 0 if node_role == "primary" else 10),
        api_token=_env_str("API_TOKEN", ""),
        host=_env_str("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8787),
        public_base_url=_env_str("PUBLIC_BASE_URL", "http://localhost:8787").rstrip("/"),
        primary_url=_env_str("PRIMARY_URL", "").rstrip("/"),
        peer_urls=_parse_url_list(_env_str("PEER_URLS", "")),
    )

    notebooklm = NotebookLMConfig(
        profile=_env_str("NOTEBOOKLM_PROFILE", "default") or "default",
        storage_path=_resolve_optional_path(_env_str("NOTEBOOKLM_STORAGE_PATH", ""), resolved_base),
        auth_json=_env_str("NOTEBOOKLM_AUTH_JSON", ""),
        auth_b64=_env_str("NOTEBOOKLM_AUTH_B64", ""),
    )

    podbean = PodbeanConfig(
        client_id=_env_str("PODBEAN_CLIENT_ID", ""),
        client_secret=_env_str("PODBEAN_CLIENT_SECRET", ""),
        publish_status=_env_str("PODBEAN_PUBLISH_STATUS", "publish") or "publish",
        episode_type=_env_str("PODBEAN_EPISODE_TYPE", "public") or "public",
    )
    telegram = _resolve_telegram_config()

    publisher_backend = _env_str("PUBLISHER_BACKEND", "podbean").lower()
    if publisher_backend not in {"podbean", "local_rss"}:
        raise ValueError(
            f"PUBLISHER_BACKEND must be 'podbean' or 'local_rss', got '{publisher_backend}'"
        )

    feeds = tuple(
        FeedConfig(
            name=str(item["name"]),
            category=str(item.get("category", "General")),
            url=str(item["url"]),
            weight=float(item.get("weight", 1.0)),
        )
        for item in feeds_raw
        if isinstance(item, dict) and item.get("url") and item.get("name")
    )

    return AppConfig(
        base_dir=resolved_base,
        storage=storage,
        podcast=podcast,
        schedule=schedule,
        harvester=harvester,
        failover=failover,
        notebooklm=notebooklm,
        podbean=podbean,
        telegram=telegram,
        publisher_backend=publisher_backend,
        ffmpeg_bin=_env_str("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
        feeds=feeds,
    )

