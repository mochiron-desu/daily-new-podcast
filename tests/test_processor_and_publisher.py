"""Tests for ffmpeg audio mastering, Podbean API workflow, and Local RSS feed builder."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.config import PodcastConfig, PodbeanConfig, load_config
from src.memory import StateDB
from src.processor import master_episode_audio
from src.publisher import LocalRssPublisher, PodbeanPublisher


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg binary not installed")
def test_ffmpeg_master_real_audio(tmp_path: Path) -> None:
    raw_wav = tmp_path / "tone.wav"
    out_mp3 = tmp_path / "episodes" / "2026-11-10.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            str(raw_wav),
        ],
        check=True,
        capture_output=True,
    )

    mastered = master_episode_audio(
        raw_audio_path=raw_wav,
        output_mp3_path=out_mp3,
        episode_title="Daily Brief — 2026-11-10",
        episode_date=date(2026, 11, 10),
        podcast_cfg=PodcastConfig(),
    )
    assert mastered.is_file()
    assert mastered.stat().st_size > 1000


async def test_podbean_publisher_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio_file = tmp_path / "2026-11-10.mp3"
    audio_file.write_bytes(b"ID3" + b"\x00" * 512)

    calls: list[tuple[str, str]] = []
    posted_contents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        if request.url.path.endswith("/episodes") and request.method == "GET":
            return httpx.Response(
                200,
                json={"episodes": [{"id": "ep-old", "title": "Daily Brief — 2026-11-09"}]},
            )
        if request.url.path.endswith("/files/uploadAuthorize"):
            return httpx.Response(
                200,
                json={
                    "presigned_url": "https://s3.podbean.test/upload/abc",
                    "file_key": "media-key-999",
                },
            )
        if "s3.podbean.test" in str(request.url) and request.method == "PUT":
            return httpx.Response(200)
        if request.url.path.endswith("/episodes") and request.method == "POST":
            from urllib.parse import parse_qs

            form = parse_qs(request.content.decode("utf-8"))
            posted_contents.append((form.get("content") or [""])[0])
            return httpx.Response(
                200,
                json={
                    "episode": {
                        "id": "ep-2026-11-10",
                        "title": "Daily Brief — 2026-11-10",
                        "media_url": "https://cdn.podbean.test/2026-11-10.mp3",
                        "permalink_url": "https://podbean.test/ep-2026-11-10",
                    }
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_async_client(transport=transport, *args, **kwargs),
    )

    pub = PodbeanPublisher(PodbeanConfig(client_id="cid", client_secret="csec"))
    existing = await pub.has_episode_for_date(date(2026, 11, 10), "Daily Brief — 2026-11-10")
    assert existing is None

    published = await pub.publish(
        audio_path=audio_file,
        title="Daily Brief — 2026-11-10",
        description="Long show notes " * 60,
        episode_date=date(2026, 11, 10),
    )
    assert published.remote_id == "ep-2026-11-10"
    assert published.media_url == "https://cdn.podbean.test/2026-11-10.mp3"
    assert any(method == "PUT" for method, _ in calls)
    assert len(posted_contents) == 1
    assert len(posted_contents[0]) <= 500


async def test_local_rss_publisher_creates_feed_xml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PUBLISHER_BACKEND", "local_rss")
    cfg = load_config(base_dir=Path(__file__).resolve().parent.parent)
    db = StateDB(cfg.storage.db_path)

    src_mp3 = tmp_path / "src.mp3"
    src_mp3.write_bytes(b"ID3testbytes")

    pub = LocalRssPublisher(cfg, db)
    res = await pub.publish(
        audio_path=src_mp3,
        title="Daily Brief — 2026-11-10",
        description="Summary",
        episode_date=date(2026, 11, 10),
    )
    assert res.media_url.endswith("/episodes/2026-11-10.mp3")
    assert cfg.storage.feed_xml_path.is_file()
    xml_text = cfg.storage.feed_xml_path.read_text(encoding="utf-8")
    assert "<enclosure" in xml_text
    assert "Daily Brief — 2026-11-10" in xml_text

