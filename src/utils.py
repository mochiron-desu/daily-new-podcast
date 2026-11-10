"""Cross-platform filesystem, subprocess, and text normalization utilities."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "more",
        "new",
        "of",
        "on",
        "or",
        "over",
        "says",
        "said",
        "that",
        "the",
        "their",
        "this",
        "to",
        "up",
        "was",
        "were",
        "will",
        "with",
    }
)


def reconfigure_stdio_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 on Windows to avoid cp1252 crashes."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class _BelowWarningFilter(logging.Filter):
    """Allow only log records below WARNING (DEBUG/INFO) onto stdout."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def configure_logging(level: str = "INFO") -> None:
    """Route DEBUG/INFO logs to stdout and WARNING+ to stderr for clean cloud log tagging."""
    reconfigure_stdio_utf8()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.addFilter(_BelowWarningFilter())
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(max(numeric_level, logging.WARNING))
    stderr_handler.setFormatter(formatter)

    logging.basicConfig(
        level=numeric_level,
        handlers=[stdout_handler, stderr_handler],
        force=True,
    )


def build_uvicorn_log_config(level: str = "INFO") -> dict[str, Any]:
    """Return uvicorn LOGGING_CONFIG routing INFO lifecycle logs to stdout instead of stderr."""
    import copy
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    upper_level = level.upper()
    cfg["filters"] = {
        "below_warning": {"()": f"{__name__}._BelowWarningFilter"},
    }
    cfg["handlers"]["default"] = {
        "formatter": "default",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "filters": ["below_warning"],
    }
    cfg["handlers"]["default_err"] = {
        "formatter": "default",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stderr",
        "level": "WARNING",
    }
    cfg["loggers"]["uvicorn"] = {
        "handlers": ["default", "default_err"],
        "level": upper_level,
        "propagate": False,
    }
    cfg["loggers"]["uvicorn.error"] = {"level": upper_level}
    cfg["loggers"]["uvicorn.access"]["level"] = upper_level
    return cfg


def subprocess_silent_kwargs() -> dict[str, Any]:
    """Return subprocess keyword args that suppress console window flashes on Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def ensure_dir(path: Path) -> Path:
    """Create directory tree if absent and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write bytes atomically using a temporary sibling file and os.replace."""
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    """Write UTF-8 text atomically."""
    return atomic_write_bytes(path, content.encode(encoding))


def now_in_timezone(timezone_name: str) -> datetime:
    """Return current timezone-aware datetime in the configured IANA timezone."""
    return datetime.now(ZoneInfo(timezone_name))


def tokenize_keywords(text: str) -> set[str]:
    """Extract normalized non-stopword alphanumeric keywords for similarity matching."""
    words = re.findall(r"[a-z0-9]{2,}", text.lower())
    return {word for word in words if word not in _STOPWORDS}


def jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Compute Jaccard overlap between two keyword sets."""
    if not tokens_a or not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def slugify(text: str, max_len: int = 64) -> str:
    """Build a compact URL/DB-safe slug from text."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        return "untitled"
    return slug[:max_len].strip("-")

