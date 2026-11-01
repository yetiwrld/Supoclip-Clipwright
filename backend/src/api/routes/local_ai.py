"""Local AI status endpoints.

The UI needs to answer real questions, not show a hardcoded green dot:
is Ollama reachable, is the configured model pulled, is the Whisper
checkpoint on disk, and is the machine ready to process media with no
internet connection. Every value below comes from an actual probe.

The module also owns the Offline Mode switch. ``.env`` decides the default;
Settings can persist a runtime override so a creator can cut the machine off
from every hosted provider immediately, without editing files or restarting.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from ... import offline_mode as offline_mode_state
from ...auth_headers import get_authenticated_user_id
from ...config import get_config
from ...local_ai import local_ai_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/local-ai", tags=["local-ai"])


class OfflineModeUpdate(BaseModel):
    """Body for the runtime switch.

    ``None`` clears the override and falls back to the ``.env`` value. Only real
    booleans are accepted, so ``"off"`` or ``1`` cannot be mistaken for a
    deliberate choice about where the user's footage is allowed to go.
    """

    offline_mode: bool | None = None

    @field_validator("offline_mode", mode="before")
    @classmethod
    def _require_boolean(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        raise ValueError("offline_mode must be true, false, or null.")


@router.get("/status")
async def get_local_ai_status(request: Request):
    """Return offline-readiness for transcription, LLM and FFmpeg."""
    # Signed-in users only, same guards as the rest of the API.
    get_authenticated_user_id(request, get_config())
    return local_ai_status(get_config())


@router.post("/offline-mode")
async def set_offline_mode(payload: OfflineModeUpdate, request: Request):
    """Persist the runtime Offline Mode switch and return fresh status.

    Hosted providers are *not* enabled by turning offline mode off: that also
    requires ``ALLOW_HOSTED_AI=true`` and a configured key, so this endpoint can
    only tighten the local-only guarantee on its own.
    """
    get_authenticated_user_id(request, get_config())
    try:
        offline_mode_state.write_override(payload.offline_mode)
    except OSError as exc:
        logger.error("Failed to persist offline mode switch: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not save the offline mode setting. Check that the data "
                "directory is writable."
            ),
        ) from exc

    config = get_config()
    logger.info(
        "Offline mode is now %s (source=%s)",
        config.offline_mode,
        config.offline_mode_source,
    )
    return local_ai_status(config)
