"""Tests for Telegram credential resolution and best-effort stage notifications."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.config import TelegramConfig, load_config
from src.notifications import TelegramNotifier, check_telegram_credentials


def test_load_config_reads_telegram_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999888")
    cfg = load_config(base_dir=Path(__file__).resolve().parent.parent)
    assert cfg.telegram.enabled is True
    assert cfg.telegram.bot_token == "123:ABC"
    assert cfg.telegram.chat_id == "999888"
    assert cfg.telegram.source == "env"


def test_load_config_falls_back_to_telegram_notify_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    notify_cfg = tmp_path / ".gemini" / "config" / "skills" / "telegram-notify" / "config.json"
    notify_cfg.parent.mkdir(parents=True, exist_ok=True)
    notify_cfg.write_text(json.dumps({"bot_token": "tok-json", "chat_id": "4242"}), encoding="utf-8")

    # Pass non-existent env_file so repo .env does not mask the mocked home fallback.
    cfg = load_config(
        base_dir=Path(__file__).resolve().parent.parent,
        env_file=tmp_path / "nonexistent.env",
    )
    assert cfg.telegram.enabled is True
    assert cfg.telegram.bot_token == "tok-json"
    assert cfg.telegram.chat_id == "4242"
    assert str(notify_cfg) in cfg.telegram.source


async def test_telegram_notifier_sends_and_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content.decode("utf-8"))
            sent_payloads.append(payload)
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "news_bot"}})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=transport, *args, **kwargs),
    )

    tg_cfg = TelegramConfig(bot_token="123:XYZ", chat_id="555", source="env")
    notifier = TelegramNotifier(tg_cfg)
    ok = await notifier.send_stage(
        "News Harvested",
        "Clusters: 12",
        status="info",
        episode_date=date(2026, 11, 10),
        node_id="railway-primary",
    )
    assert ok is True
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["chat_id"] == "555"
    assert "News Harvested" in sent_payloads[0]["text"]

    live_ok, live_msg = await check_telegram_credentials(tg_cfg, verify_live=True)
    assert live_ok is True
    assert "@news_bot" in live_msg

    # Network errors must not propagate
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    fail_transport = httpx.MockTransport(fail_handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=fail_transport, *args, **kwargs),
    )
    assert await notifier.send_stage("Published", "Done", status="success") is False


async def test_telegram_notifier_send_audio_and_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request.url.path)
        if request.url.path.endswith("/sendAudio") or request.url.path.endswith("/sendDocument"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=transport, *args, **kwargs),
    )

    tg_cfg = TelegramConfig(bot_token="123:XYZ", chat_id="555", source="env", send_artifacts=True)
    notifier = TelegramNotifier(tg_cfg)

    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"dummy audio content")
    doc_file = tmp_path / "dossier.pdf"
    doc_file.write_bytes(b"%PDF-1.4 dummy pdf content")

    audio_ok = await notifier.send_audio(
        audio_file,
        caption="Test Episode",
        title="Episode 1",
        performer="Daily News Podcast",
    )
    assert audio_ok is True
    assert any("/sendAudio" in p for p in sent_requests)

    doc_ok = await notifier.send_document(
        doc_file,
        caption="News Dossier",
    )
    assert doc_ok is True
    assert any("/sendDocument" in p for p in sent_requests)

    # Missing file returns False without raising
    non_existent = tmp_path / "missing.mp3"
    assert await notifier.send_audio(non_existent) is False
    assert await notifier.send_document(non_existent) is False

