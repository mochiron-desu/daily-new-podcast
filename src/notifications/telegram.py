"""Best-effort Telegram Bot API notifier for podcast pipeline stages."""

from __future__ import annotations

import logging
from datetime import date

import httpx

from ..config import TelegramConfig

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {
    "info": "ℹ️",
    "running": "⏳",
    "success": "✅",
    "warning": "⚠️",
    "failure": "❌",
}


def _escape_markdown(text: str) -> str:
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


class TelegramNotifier:
    """Async best-effort stage logger using Telegram Bot API."""

    def __init__(self, cfg: TelegramConfig, timeout_seconds: float = 8.0) -> None:
        self.cfg = cfg
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    async def _post_message(self, text: str, parse_mode: str | None = "Markdown") -> bool:
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        payload: dict[str, object] = {
            "chat_id": self.cfg.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            res = await client.post(url, json=payload)
            if res.status_code == 400 and parse_mode and "can't parse entities" in res.text.lower():
                return await self._post_message(text, parse_mode=None)
            res.raise_for_status()
            data = res.json()
            return bool(data.get("ok"))

    async def send_stage(
        self,
        stage: str,
        detail: str = "",
        *,
        status: str = "info",
        episode_date: date | str | None = None,
        node_id: str = "",
    ) -> bool:
        """Send a stage log notification without ever raising to pipeline callers."""
        if not self.enabled:
            return False

        icon = _STATUS_EMOJI.get(status, "🔔")
        header = f"{icon} *Daily Brief — {_escape_markdown(stage)}*"
        meta_parts: list[str] = []
        if episode_date is not None:
            ep_str = episode_date.isoformat() if isinstance(episode_date, date) else str(episode_date)
            meta_parts.append(f"📅 `{ep_str}`")
        if node_id:
            meta_parts.append(f"🖥️ `{node_id}`")

        lines = [header]
        if meta_parts:
            lines.append(" | ".join(meta_parts))
        if detail.strip():
            lines.extend(["", detail.strip()])

        message = "\n".join(lines)
        try:
            return await self._post_message(message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Telegram notification skipped (%s): %s", stage, exc)
            return False


async def check_telegram_credentials(
    cfg: TelegramConfig,
    verify_live: bool = False,
) -> tuple[bool, str]:
    """Check whether Telegram credentials are configured and optionally probe getMe."""
    if not cfg.enabled:
        return False, "Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (and no telegram-notify config)"
    if not verify_live:
        return True, f"Configured via {cfg.source}"

    url = f"https://api.telegram.org/bot{cfg.bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
        if not data.get("ok"):
            return False, f"Telegram getMe returned not-ok ({cfg.source})"
        username = (data.get("result") or {}).get("username") or "bot"
        return True, f"Authenticated @{username} via {cfg.source}"
    except Exception as exc:
        return False, f"Telegram live check failed ({cfg.source}): {exc}"

