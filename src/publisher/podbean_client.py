"""Podbean REST API v1 OAuth2 client with remote episode deduplication."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx

from ..config import PodbeanConfig
from .base import PublishedEpisode, PublisherBackend, PublisherError

logger = logging.getLogger(__name__)


class PodbeanPublisher(PublisherBackend):
    """Podbean OAuth2 client-credentials uploader + episode deduplication checker."""

    def __init__(self, cfg: PodbeanConfig, timeout_seconds: float = 60.0) -> None:
        self.cfg = cfg
        self.base_url = cfg.api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _require_credentials(self) -> None:
        if not self.cfg.client_id or not self.cfg.client_secret:
            raise PublisherError(
                "Podbean credentials missing. Set PODBEAN_CLIENT_ID and PODBEAN_CLIENT_SECRET."
            )

    async def _fetch_access_token(self, client: httpx.AsyncClient) -> str:
        self._require_credentials()
        res = await client.post(
            f"{self.base_url}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.cfg.client_id, self.cfg.client_secret),
        )
        if res.status_code >= 400:
            raise PublisherError(f"Podbean OAuth failed ({res.status_code}): {res.text}")
        payload = res.json()
        token = payload.get("access_token")
        if not token:
            raise PublisherError(f"Podbean OAuth response missing access_token: {payload}")
        return str(token)

    async def check_credentials(self) -> tuple[bool, str]:
        if not self.cfg.client_id or not self.cfg.client_secret:
            return False, "PODBEAN_CLIENT_ID / PODBEAN_CLIENT_SECRET not configured"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                token = await self._fetch_access_token(client)
                res = await client.get(
                    f"{self.base_url}/episodes",
                    params={"access_token": token, "limit": 1, "offset": 0},
                )
                res.raise_for_status()
            return True, "Podbean OAuth & episode listing verified"
        except Exception as exc:
            return False, f"Podbean verification failed: {exc}"

    async def has_episode_for_date(
        self,
        episode_date: date,
        expected_title: str,
    ) -> PublishedEpisode | None:
        if not self.cfg.client_id or not self.cfg.client_secret:
            return None

        date_marker = episode_date.isoformat()
        async with httpx.AsyncClient(timeout=25.0) as client:
            token = await self._fetch_access_token(client)
            res = await client.get(
                f"{self.base_url}/episodes",
                params={"access_token": token, "limit": 20, "offset": 0},
            )
            if res.status_code >= 400:
                raise PublisherError(
                    f"Podbean list episodes failed ({res.status_code}): {res.text}"
                )
            data = res.json()

        for ep in data.get("episodes", []) or []:
            ep_title = str(ep.get("title") or "")
            if ep_title == expected_title or date_marker in ep_title:
                return PublishedEpisode(
                    remote_id=str(ep.get("id") or ""),
                    title=ep_title,
                    media_url=str(ep.get("media_url") or ""),
                    permalink_url=str(ep.get("permalink_url") or ""),
                )
        return None

    async def publish(
        self,
        *,
        audio_path: Path,
        title: str,
        description: str,
        episode_date: date,
    ) -> PublishedEpisode:
        if not audio_path.is_file():
            raise PublisherError(f"Audio file to upload does not exist: {audio_path}")

        filesize = audio_path.stat().st_size
        filename = audio_path.name

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, write=300.0)) as client:
            token = await self._fetch_access_token(client)

            # 1. Authorize media upload -> presigned S3 URL + file_key
            auth_res = await client.get(
                f"{self.base_url}/files/uploadAuthorize",
                params={
                    "access_token": token,
                    "filename": filename,
                    "filesize": filesize,
                    "content_type": "audio/mpeg",
                },
            )
            if auth_res.status_code >= 400:
                raise PublisherError(
                    f"Podbean uploadAuthorize failed ({auth_res.status_code}): {auth_res.text}"
                )
            auth_payload = auth_res.json()
            presigned_url = auth_payload.get("presigned_url")
            file_key = auth_payload.get("file_key")
            if not presigned_url or not file_key:
                raise PublisherError(f"Invalid uploadAuthorize payload: {auth_payload}")

            # 2. Stream MP3 bytes asynchronously to pre-signed URL
            logger.info("Uploading %s (%d KB) to Podbean S3...", filename, filesize // 1024)

            async def _stream_audio():
                with audio_path.open("rb") as audio_handle:
                    while chunk := audio_handle.read(262_144):
                        yield chunk

            put_res = await client.put(
                presigned_url,
                content=_stream_audio(),
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Length": str(filesize),
                },
            )
            if put_res.status_code >= 400:
                raise PublisherError(
                    f"Podbean pre-signed PUT failed ({put_res.status_code}): {put_res.text}"
                )

            # 3. Publish episode with media_key
            ep_res = await client.post(
                f"{self.base_url}/episodes",
                data={
                    "access_token": token,
                    "title": title,
                    "content": description,
                    "status": self.cfg.publish_status,
                    "type": self.cfg.episode_type,
                    "media_key": file_key,
                },
            )
            if ep_res.status_code >= 400:
                raise PublisherError(
                    f"Podbean POST /episodes failed ({ep_res.status_code}): {ep_res.text}"
                )
            ep_data = ep_res.json().get("episode") or ep_res.json()
            published = PublishedEpisode(
                remote_id=str(ep_data.get("id") or file_key),
                title=str(ep_data.get("title") or title),
                media_url=str(ep_data.get("media_url") or ""),
                permalink_url=str(ep_data.get("permalink_url") or ""),
            )
            logger.info("Published episode '%s' on Podbean (id=%s)", published.title, published.remote_id)
            return published
