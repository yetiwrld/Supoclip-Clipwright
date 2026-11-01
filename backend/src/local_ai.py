"""Local AI runtime for ClipWright Studio.

Everything the clipping pipeline needs to think runs on this machine:

* transcription -> OpenAI Whisper (``openai-whisper``), checkpoint stored in a
  persistent directory,
* transcript analysis / AI editing -> Ollama (``llama3.2:3b`` by default),
* rendering -> local FFmpeg.

This module is the single place that answers "is the local stack ready?".
It is used by the API (``/local-ai/status``), by the worker preflight, and by
``scripts/setup_local_ai.py``. Nothing here ever contacts a hosted AI provider.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

import httpx

from . import offline_mode as offline_mode_state
from .config import Config, get_config

logger = logging.getLogger(__name__)

# The Whisper catalogue lives in a dependency-free module so the host setup
# script can reuse it before the backend environment exists.
from . import whisper_models
from .whisper_models import (
    DEFAULT_WHISPER_MODEL,
    WHISPER_MODEL_SIZES_MB,
    WHISPER_MODEL_URLS,
)

HOSTED_LLM_PROVIDERS = frozenset(
    {"openai", "google", "google-gla", "openrouter", "anthropic", "groq", "mistral", "xai"}
)

DEFAULT_OLLAMA_MODEL = "llama3.2:3b"

OLLAMA_PROBE_TIMEOUT_SECONDS = 2.5


class LocalModelUnavailable(RuntimeError):
    """A local model is required but not installed/loaded.

    The message is written for the creator, not for a log file: it names the
    exact command that fixes the problem.
    """


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------


def normalize_whisper_model_name(name: Optional[str]) -> str:
    """Return the canonical Whisper checkpoint name for ``name``."""
    return whisper_models.normalize_model_name(name)


def whisper_model_url(name: str) -> Optional[str]:
    return whisper_models.model_url(name)


def whisper_checkpoint_filename(name: str) -> str:
    """File name openai-whisper writes inside its download root."""
    return whisper_models.checkpoint_filename(name)


def resolve_models_dir(config: Optional[Config] = None) -> Path:
    """Persistent root for local model files (host directory or Docker volume)."""
    config = config or get_config()
    raw = getattr(config, "models_dir", None) or os.getenv("MODELS_DIR", "models")
    return Path(raw).expanduser().resolve()


def resolve_whisper_model_dir(config: Optional[Config] = None) -> Path:
    """Directory holding Whisper checkpoints (``WHISPER_MODEL_DIR``)."""
    config = config or get_config()
    raw = getattr(config, "whisper_model_dir", None) or os.getenv("WHISPER_MODEL_DIR", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return resolve_models_dir(config) / "whisper"


def whisper_model_path(
    name: Optional[str] = None, config: Optional[Config] = None
) -> Path:
    config = config or get_config()
    model_name = normalize_whisper_model_name(name or getattr(config, "whisper_model", None))
    return resolve_whisper_model_dir(config) / whisper_checkpoint_filename(model_name)


def whisper_model_status(config: Optional[Config] = None) -> dict[str, Any]:
    """Describe the configured Whisper checkpoint without loading torch."""
    config = config or get_config()
    model_name = normalize_whisper_model_name(getattr(config, "whisper_model", None))
    path = whisper_model_path(model_name, config)
    known = model_name in WHISPER_MODEL_URLS
    exists = path.is_file() and path.stat().st_size > 0
    size_mb = round(path.stat().st_size / (1024 * 1024), 1) if exists else 0.0
    expected_mb = WHISPER_MODEL_SIZES_MB.get(model_name)
    message = None
    if not exists:
        install_cmd = "python scripts/setup_local_ai.py --whisper-model " + model_name
        message = (
            f"Whisper model '{model_name}' is not installed in {path.parent}. "
            f"Run: {install_cmd}"
        )
    return {
        "model": model_name,
        "path": str(path),
        "directory": str(path.parent),
        "available": exists,
        "known_model": known,
        "size_mb": size_mb,
        "expected_size_mb": expected_mb,
        "download_url": whisper_model_url(model_name),
        "message": message,
    }


def ensure_whisper_model(config: Optional[Config] = None) -> Path:
    """Return the checkpoint path, or raise a creator-facing error."""
    status = whisper_model_status(config)
    if not status["available"]:
        raise LocalModelUnavailable(status["message"])
    if not status["known_model"]:
        raise LocalModelUnavailable(
            f"Unknown Whisper model '{status['model']}'. "
            "Use one of: " + ", ".join(sorted(WHISPER_MODEL_URLS))
        )
    return Path(status["path"])


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def split_model_name(model_name: Optional[str]) -> tuple[str, Optional[str]]:
    """Split ``provider:model`` the same way the analysis layer does."""
    raw = (model_name or "").strip()
    if not raw:
        return "", None
    if ":" not in raw:
        return raw.lower(), None
    provider, rest = raw.split(":", 1)
    return provider.strip().lower(), rest.strip() or None


def llm_provider(config: Optional[Config] = None) -> str:
    config = config or get_config()
    provider, _ = split_model_name(getattr(config, "llm", None))
    return provider


def ollama_model_name(config: Optional[Config] = None) -> Optional[str]:
    """The Ollama model id (without the ``ollama:`` prefix)."""
    config = config or get_config()
    provider, name = split_model_name(getattr(config, "llm", None))
    return name if provider == "ollama" else None


def native_ollama_base_url(config: Optional[Config] = None) -> str:
    """Ollama's native API root, derived from the OpenAI-compatible URL.

    ``OLLAMA_BASE_URL`` is stored with the ``/v1`` suffix because pydantic-ai
    and the OpenAI-compatible clients expect it. ``/api/tags`` and
    ``/api/chat`` live on the bare root.
    """
    config = config or get_config()
    base = (config.resolve_ollama_base_url() or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base or "http://localhost:11434"


def _normalize_model_id(name: str) -> str:
    name = (name or "").strip().lower()
    return name if ":" in name else f"{name}:latest"


def check_ollama(
    config: Optional[Config] = None, timeout: float = OLLAMA_PROBE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Probe the local Ollama daemon and list its installed models."""
    config = config or get_config()
    base = native_ollama_base_url(config)
    result: dict[str, Any] = {
        "base_url": base,
        "openai_base_url": f"{base}/v1",
        "reachable": False,
        "models": [],
        "error": None,
    }
    try:
        response = httpx.get(f"{base}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        result["models"] = sorted(
            {
                str(model.get("name") or model.get("model") or "").strip()
                for model in (models or [])
                if isinstance(model, dict)
            }
            - {""}
        )
        result["reachable"] = True
    except httpx.HTTPError as exc:  # connection refused, timeout, bad status
        logger.debug("Ollama probe failed at %s: %s", base, exc)
        result["error"] = str(exc) or exc.__class__.__name__
    except ValueError as exc:  # malformed JSON
        logger.debug("Ollama returned an unreadable model list: %s", exc)
        result["reachable"] = True
        result["error"] = "Ollama returned an unreadable model list."
    return result


def model_is_installed(installed: list[str], model: str) -> bool:
    wanted = _normalize_model_id(model)
    return any(_normalize_model_id(item) == wanted for item in installed)


def ollama_setup_hint(model: Optional[str]) -> str:
    model = model or DEFAULT_OLLAMA_MODEL
    return (
        f"Install Ollama (https://ollama.com/download), start it, then run: "
        f"ollama pull {model}"
    )


def llm_status(config: Optional[Config] = None) -> dict[str, Any]:
    """Status of the configured language model, local or explicitly hosted."""
    config = config or get_config()
    llm = getattr(config, "llm", None) or ""
    provider, model = split_model_name(llm)
    offline = bool(getattr(config, "offline_mode", True))
    allow_hosted = bool(getattr(config, "allow_hosted_ai", False))
    status: dict[str, Any] = {
        "configured_model": llm,
        "provider": provider or None,
        "model": model,
        "local": provider == "ollama",
        "offline_mode": offline,
        "allow_hosted_ai": allow_hosted,
    }
    if provider != "ollama":
        hosted_allowed = allow_hosted and not offline
        status.update(
            {
                "reachable": hosted_allowed,
                "model_available": hosted_allowed,
                "installed_models": [],
                "message": None
                if hosted_allowed
                else (
                    "ClipWright Studio is in offline mode. Hosted AI providers are disabled; "
                    f"set LLM=ollama:{DEFAULT_OLLAMA_MODEL} to run locally."
                ),
                "requires_internet": True,
            }
        )
        return status

    probe = check_ollama(config)
    installed = probe["models"]
    model_available = bool(model) and probe["reachable"] and model_is_installed(installed, model or "")
    message = None
    if not probe["reachable"]:
        message = (
            "Ollama is not reachable at "
            f"{probe['base_url']}. {ollama_setup_hint(model)}"
        )
    elif not model:
        message = "No Ollama model is configured. Set LLM=ollama:llama3.2:3b."
    elif not model_available:
        message = f"Ollama is running but model '{model}' is not installed. Run: ollama pull {model}"
    status.update(
        {
            "base_url": probe["base_url"],
            "openai_base_url": probe["openai_base_url"],
            "reachable": probe["reachable"],
            "model_available": model_available,
            "installed_models": installed,
            "message": message,
            "requires_internet": False,
            "probe_error": probe["error"],
        }
    )
    return status


def ensure_ollama_ready(config: Optional[Config] = None) -> None:
    """Raise a creator-facing error unless the configured local LLM is usable."""
    status = llm_status(config)
    if not status.get("local"):
        if status.get("model_available"):
            return
        raise LocalModelUnavailable(status.get("message") or "Hosted AI is disabled in offline mode.")
    if not status.get("model_available"):
        raise LocalModelUnavailable(status.get("message") or "The local Ollama model is not available.")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def check_ffmpeg() -> dict[str, Any]:
    path = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {
        "available": bool(path),
        "path": path or "",
        "ffprobe_available": bool(ffprobe),
        "message": None if path else "FFmpeg was not found on PATH. Rendering and audio extraction need it.",
    }


def optional_online_features(config: Optional[Config] = None) -> list[dict[str, Any]]:
    """Features that still need the internet, so the UI can label them honestly."""
    config = config or get_config()
    return [
        {
            "id": "stream_downloads",
            "label": "YouTube / Twitch / Kick downloads",
            "requires_internet": True,
            "detail": "Downloading a link needs the internet. Local files never do.",
        },
        {
            "id": "broll",
            "label": "Pexels stock B-roll",
            "requires_internet": True,
            "configured": bool(getattr(config, "pexels_api_key", None)),
            "detail": "Stock footage lookups call the Pexels API. Existing B-roll keeps rendering offline.",
        },
        {
            "id": "social_publishing",
            "label": "Social publishing & analytics",
            "requires_internet": True,
            "configured": getattr(config, "social_publishing_access", "off") != "off",
            "detail": "Uploading to YouTube, TikTok or Instagram needs their servers.",
        },
        {
            "id": "hosted_ai",
            "label": "Hosted AI providers (OpenAI, Anthropic, Google, OpenRouter)",
            "requires_internet": True,
            "configured": bool(getattr(config, "allow_hosted_ai", False))
            and not bool(getattr(config, "offline_mode", True)),
            "detail": "Optional and disabled by default. Local Whisper and Ollama cover the workflow.",
        },
    ]


def preflight_task(config: Optional[Config] = None) -> list[str]:
    """Problems that would make a local clipping job fail, in plain language.

    Called by the worker before the pipeline starts so a missing model is
    reported immediately instead of twenty minutes into a render.
    """
    config = config or get_config()
    problems: list[str] = []

    provider = getattr(config, "transcription_provider", "whisper")
    if provider == "whisper":
        whisper = whisper_model_status(config)
        if not whisper["available"]:
            problems.append(str(whisper["message"]))
    elif provider == "assemblyai":
        if getattr(config, "offline_mode", True) or not getattr(config, "allow_hosted_ai", False):
            problems.append(
                "TRANSCRIPTION_PROVIDER=assemblyai is a hosted service and is disabled offline. "
                "Set TRANSCRIPTION_PROVIDER=whisper."
            )
        elif not getattr(config, "assembly_ai_api_key", None):
            problems.append("TRANSCRIPTION_PROVIDER=assemblyai needs ASSEMBLY_AI_API_KEY.")

    status = llm_status(config)
    if not status.get("model_available"):
        problems.append(str(status.get("message") or "The configured language model is unavailable."))

    if not check_ffmpeg()["available"]:
        problems.append("FFmpeg was not found on PATH; rendering needs it.")

    return problems


def local_ai_status(config: Optional[Config] = None) -> dict[str, Any]:
    """Complete offline-readiness snapshot for the UI and setup scripts."""
    config = config or get_config()
    whisper = whisper_model_status(config)
    llm = llm_status(config)
    ffmpeg = check_ffmpeg()
    transcription = {
        "provider": getattr(config, "transcription_provider", "whisper"),
        "whisper": whisper,
    }
    ready = bool(whisper["available"]) and bool(llm["model_available"]) and ffmpeg["available"]
    return {
        "app": "ClipWright Studio",
        "offline_mode": bool(getattr(config, "offline_mode", True)),
        # Which layer decided offline mode: the .env default, or the runtime
        # switch persisted from Settings.
        "offline_mode_source": getattr(config, "offline_mode_source", "env"),
        "offline_mode_from_env": bool(getattr(config, "offline_mode_from_env", True)),
        "offline_mode_state_path": str(offline_mode_state.state_path()),
        "allow_hosted_ai": bool(getattr(config, "allow_hosted_ai", False)),
        "hosted_ai_available": bool(config.hosted_ai_enabled),
        "local_only": not bool(config.hosted_ai_enabled),
        "ready_for_offline_processing": ready,
        "transcription": transcription,
        "llm": llm,
        "runtime": {"ffmpeg": ffmpeg},
        "optional_online_features": optional_online_features(config),
        "checks": [
            {
                "id": "whisper_model",
                "label": f"Whisper model ({whisper['model']})",
                "ok": whisper["available"],
                "detail": whisper["message"] or f"Ready at {whisper['path']}",
            },
            {
                "id": "ollama",
                "label": "Ollama daemon",
                "ok": bool(llm.get("reachable")),
                "detail": llm.get("message") or f"Reachable at {llm.get('base_url')}",
            },
            {
                "id": "llm_model",
                "label": f"Language model ({llm.get('model') or 'not set'})",
                "ok": bool(llm.get("model_available")),
                "detail": llm.get("message") or "Model installed",
            },
            {
                "id": "ffmpeg",
                "label": "FFmpeg",
                "ok": ffmpeg["available"],
                "detail": ffmpeg["message"] or ffmpeg["path"],
            },
        ],
    }
