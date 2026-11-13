"""Ephemeral Google NotebookLM audio generator with Railway + local auth support."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from notebooklm import AuthError, AuthExtractionError, NotebookLMClient
from notebooklm.exceptions import ConfigurationError
from notebooklm.types import AudioFormat, AudioLength

from ..config import NotebookLMConfig, PodcastConfig
from ..utils import atomic_write_text, ensure_dir

logger = logging.getLogger(__name__)

_AUDIO_FORMAT_MAP = {
    "deep-dive": AudioFormat.DEEP_DIVE,
    "deep_dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}

_AUDIO_LENGTH_MAP = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}


class NotebookLMAuthError(RuntimeError):
    """Raised when Google NotebookLM authentication is missing or expired."""


class NotebookLMGenerationError(RuntimeError):
    """Raised when NotebookLM source upload or audio generation fails."""


@dataclass(frozen=True)
class AuthCheckResult:
    ok: bool
    mode: str
    detail: str
    storage_path: Path | None = None


def _decode_auth_payload(cfg: NotebookLMConfig) -> str | None:
    if cfg.auth_b64:
        try:
            decoded = base64.b64decode(cfg.auth_b64).decode("utf-8").strip()
        except Exception as exc:
            raise NotebookLMAuthError(f"Invalid base64 in NOTEBOOKLM_AUTH_B64: {exc}") from exc
        if decoded:
            return decoded
    if cfg.auth_json:
        return cfg.auth_json
    return None


def _validate_storage_json(raw_json: str, source_label: str) -> None:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise NotebookLMAuthError(f"Malformed JSON in {source_label}: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cookies"), list):
        raise NotebookLMAuthError(
            f"{source_label} must contain a Playwright storage_state JSON object with 'cookies'."
        )


def _decode_master_token_payload(cfg: NotebookLMConfig) -> str | None:
    if cfg.master_token_b64:
        try:
            decoded = base64.b64decode(cfg.master_token_b64).decode("utf-8").strip()
        except Exception as exc:
            raise NotebookLMAuthError(f"Invalid base64 in NOTEBOOKLM_MASTER_TOKEN_B64: {exc}") from exc
        if decoded:
            return decoded
    if cfg.master_token_json:
        return cfg.master_token_json
    return None


def _validate_master_token_json(raw_json: str, source_label: str) -> None:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise NotebookLMAuthError(f"Malformed JSON in {source_label}: {exc}") from exc
    if not isinstance(parsed, dict) or "master_token" not in parsed:
        raise NotebookLMAuthError(
            f"{source_label} must contain a Google Master Token JSON object with 'master_token'."
        )


def resolve_notebooklm_storage(
    cfg: NotebookLMConfig,
    storage_root: Path,
    prefer_writable_seed: bool = False,
) -> Path | None:
    """Resolve storage_state.json path or env-backed auth for NotebookLMClient.

    Precedence:
    1. Empty NOTEBOOKLM_AUTH_JSON env vars are sanitized so notebooklm-py doesn't choke.
    2. If NOTEBOOKLM_MASTER_TOKEN_B64 / NOTEBOOKLM_MASTER_TOKEN_JSON is set, write/update
       master_token.json sibling to the storage path.
    3. If NOTEBOOKLM_AUTH_B64 / NOTEBOOKLM_AUTH_JSON is set and a writable path is
       configured (or prefer_writable_seed=True, e.g. Railway Volume), seed/update the
       file whenever the env secret hash changes so cookie rotation still works on disk.
    4. If NOTEBOOKLM_STORAGE_PATH exists (or has a sibling master_token.json), use it directly.
    5. If NOTEBOOKLM_AUTH_JSON is set (inline), return None so notebooklm-py reads it.
    6. Otherwise fall back to local profile (~/.notebooklm/profiles/<profile>/storage_state.json).
    """
    if os.environ.get("NOTEBOOKLM_AUTH_JSON", None) == "":
        os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)

    env_payload = _decode_auth_payload(cfg)
    master_payload = _decode_master_token_payload(cfg)
    target_path = cfg.storage_path
    if target_path is None and (env_payload or master_payload) and prefer_writable_seed:
        target_path = storage_root / "notebooklm_storage_state.json"

    # Seed master_token.json sibling if master token payload provided
    if master_payload:
        _validate_master_token_json(master_payload, "NOTEBOOKLM_MASTER_TOKEN_B64/NOTEBOOKLM_MASTER_TOKEN_JSON")
        effective_target = target_path or (storage_root / "notebooklm_storage_state.json")
        ensure_dir(effective_target.parent)
        master_path = effective_target.with_name("master_token.json")
        payload_hash = hashlib.sha256(master_payload.encode("utf-8")).hexdigest()
        hash_file = master_path.with_suffix(".sha256")
        current_hash = hash_file.read_text(encoding="utf-8").strip() if hash_file.exists() else ""
        if not master_path.exists() or current_hash != payload_hash:
            atomic_write_text(master_path, master_payload)
            hash_file.write_text(payload_hash, encoding="utf-8")
            if sys.platform != "win32":
                with contextlib.suppress(OSError):
                    os.chmod(master_path, 0o600)
        target_path = effective_target

    if env_payload:
        _validate_storage_json(env_payload, "NOTEBOOKLM_AUTH_B64/NOTEBOOKLM_AUTH_JSON")
        if target_path is not None:
            ensure_dir(target_path.parent)
            payload_hash = hashlib.sha256(env_payload.encode("utf-8")).hexdigest()
            hash_file = target_path.with_suffix(".sha256")
            current_hash = hash_file.read_text(encoding="utf-8").strip() if hash_file.exists() else ""
            if not target_path.exists() or current_hash != payload_hash:
                atomic_write_text(target_path, env_payload)
                atomic_write_text(hash_file, payload_hash)
            return target_path
        # Expose decoded JSON (from NOTEBOOKLM_AUTH_B64 too) for notebooklm-py env loader.
        os.environ["NOTEBOOKLM_AUTH_JSON"] = env_payload
        return None

    if target_path is not None:
        master_sibling = target_path.with_name("master_token.json")
        if not target_path.exists() and not master_sibling.exists():
            raise NotebookLMAuthError(f"NOTEBOOKLM_STORAGE_PATH does not exist: {target_path}")
        return target_path

    default_profile_path = (
        Path.home() / ".notebooklm" / "profiles" / cfg.profile / "storage_state.json"
    )
    if default_profile_path.exists() or default_profile_path.with_name("master_token.json").exists():
        return default_profile_path
    legacy_path = Path.home() / ".notebooklm" / "storage_state.json"
    if legacy_path.exists() or legacy_path.with_name("master_token.json").exists():
        return legacy_path
    raise NotebookLMAuthError(
        "No NotebookLM credentials found. Run 'notebooklm login' locally or set "
        "NOTEBOOKLM_MASTER_TOKEN_B64 / NOTEBOOKLM_AUTH_B64 / NOTEBOOKLM_AUTH_JSON."
    )


async def check_notebooklm_auth(
    cfg: NotebookLMConfig,
    storage_root: Path,
    prefer_writable_seed: bool = False,
    verify_live: bool = False,
) -> AuthCheckResult:
    """Validate local/env NotebookLM credentials and optionally probe notebooks.list()."""
    try:
        resolved = resolve_notebooklm_storage(
            cfg,
            storage_root=storage_root,
            prefer_writable_seed=prefer_writable_seed,
        )
    except NotebookLMAuthError as exc:
        return AuthCheckResult(ok=False, mode="unconfigured", detail=str(exc))

    mode = f"file:{resolved}" if resolved is not None else "env:NOTEBOOKLM_AUTH_JSON"
    if not verify_live:
        return AuthCheckResult(ok=True, mode=mode, detail="Credentials present", storage_path=resolved)

    try:
        async with NotebookLMClient.from_storage(
            path=str(resolved) if resolved is not None else None,
            profile=cfg.profile,
        ) as client:
            notebooks = await client.notebooks.list()
        return AuthCheckResult(
            ok=True,
            mode=mode,
            detail=f"Authenticated ({len(notebooks)} notebook(s) visible)",
            storage_path=resolved,
        )
    except (AuthError, AuthExtractionError, ConfigurationError, FileNotFoundError, ValueError) as exc:
        return AuthCheckResult(ok=False, mode=mode, detail=f"Auth invalid: {exc}", storage_path=resolved)
    except Exception as exc:
        return AuthCheckResult(ok=False, mode=mode, detail=f"Probe failed: {exc}", storage_path=resolved)


async def generate_ephemeral_audio(
    *,
    episode_date: date,
    dossier_path: Path,
    prompt_text: str,
    output_audio_path: Path,
    notebooklm_cfg: NotebookLMConfig,
    podcast_cfg: PodcastConfig,
    storage_root: Path,
    prefer_writable_seed: bool = False,
) -> Path:
    """Create ephemeral notebook, upload dossier, generate audio, download, and delete notebook."""
    if not dossier_path.exists():
        raise FileNotFoundError(f"Dossier file not found: {dossier_path}")

    resolved_storage = resolve_notebooklm_storage(
        notebooklm_cfg,
        storage_root=storage_root,
        prefer_writable_seed=prefer_writable_seed,
    )

    audio_format = _AUDIO_FORMAT_MAP.get(podcast_cfg.audio_format.lower(), AudioFormat.DEEP_DIVE)
    audio_length = _AUDIO_LENGTH_MAP.get(podcast_cfg.audio_length.lower(), AudioLength.DEFAULT)
    notebook_title = f"Daily_Brief_{episode_date.isoformat()}"
    ensure_dir(output_audio_path.parent)
    tmp_download = output_audio_path.with_suffix(".downloading.m4a")
    tmp_download.unlink(missing_ok=True)

    try:
        async with NotebookLMClient.from_storage(
            path=str(resolved_storage) if resolved_storage is not None else None,
            profile=notebooklm_cfg.profile,
        ) as client:
            notebook = await client.notebooks.create(notebook_title)
            logger.info("Created ephemeral NotebookLM notebook '%s' (%s)", notebook_title, notebook.id)
            succeeded = False
            try:
                source = await client.sources.add_file(
                    notebook.id,
                    dossier_path,
                    wait=True,
                    wait_timeout=float(podcast_cfg.source_ready_timeout_seconds),
                )
                logger.info("Source ready in NotebookLM: %s (%s)", source.title, source.id)

                gen_status = await client.artifacts.generate_audio(
                    notebook.id,
                    source_ids=[source.id],
                    language=podcast_cfg.language,
                    instructions=prompt_text,
                    audio_format=audio_format,
                    audio_length=audio_length,
                )
                logger.info("NotebookLM audio generation started (task_id=%s)", gen_status.task_id)

                final_status = await client.artifacts.wait_for_completion(
                    notebook.id,
                    gen_status.task_id,
                    timeout=float(podcast_cfg.notebook_generation_timeout_seconds),
                )
                if final_status.is_failed or not final_status.is_complete:
                    raise NotebookLMGenerationError(
                        f"Audio generation ended with status={final_status.status}: "
                        f"{final_status.error or final_status.error_code or 'unknown error'}"
                    )

                await client.artifacts.download_audio(
                    notebook.id,
                    str(tmp_download),
                    artifact_id=final_status.task_id,
                )
                if not tmp_download.exists() or tmp_download.stat().st_size == 0:
                    raise NotebookLMGenerationError("Downloaded audio file is empty.")
                os.replace(tmp_download, output_audio_path)
                succeeded = True
                logger.info(
                    "Downloaded NotebookLM audio to %s (%d KB)",
                    output_audio_path,
                    output_audio_path.stat().st_size // 1024,
                )
                return output_audio_path
            finally:
                if succeeded or podcast_cfg.delete_notebook_on_error:
                    try:
                        await client.notebooks.delete(notebook.id)
                        logger.info("Deleted ephemeral NotebookLM notebook %s", notebook.id)
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Failed to delete ephemeral notebook %s: %s",
                            notebook.id,
                            cleanup_exc,
                        )
    except (AuthError, AuthExtractionError, ConfigurationError, FileNotFoundError, ValueError) as exc:
        tmp_download.unlink(missing_ok=True)
        raise NotebookLMAuthError(f"NotebookLM authentication error: {exc}") from exc
    except NotebookLMAuthError:
        tmp_download.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_download.unlink(missing_ok=True)
        if "login" in str(exc).lower() or "auth" in str(exc).lower() or "cookie" in str(exc).lower():
            raise NotebookLMAuthError(f"NotebookLM authentication failure: {exc}") from exc
        raise NotebookLMGenerationError(f"NotebookLM audio generation failed: {exc}") from exc
