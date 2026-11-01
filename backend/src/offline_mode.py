"""Persisted runtime switch for Offline Mode.

``OFFLINE_MODE`` lives in ``.env`` and is the deploy-level default. Creators
also need a switch at runtime ("stop sending anything anywhere, now"), so the
setting is mirrored to a tiny JSON file that every process reads:

    <CLIPWRIGHT_CONFIG_DIR>/offline-mode.json   ->  {"offline_mode": false}

* The file is the *runtime* value; ``.env`` remains the source of truth for a
  fresh install. Deleting the file restores the ``.env`` value.
* The file lives on a host bind mount (``./data/config`` inside the containers),
  so the backend, the render worker and the AI worker all read the same answer
  and the choice survives restarts.
* Turning offline mode **off** never enables a hosted provider on its own: that
  still requires ``ALLOW_HOSTED_AI=true`` and a configured key. The module can
  therefore only ever *tighten* the guarantee on its own.

Reading happens on every ``Config()`` construction (the codebase builds a fresh
``Config`` per call), so a write takes effect without a restart. A small
mtime-keyed cache keeps that free in hot paths.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "offline-mode.json"

# (path, mtime_ns, size, value) — invalidated whenever the file changes.
_cache: tuple[Optional[str], Optional[int], Optional[int], Optional[bool]] = (
    None,
    None,
    None,
    None,
)


def state_path() -> Path:
    """Location of the runtime override file."""
    explicit = (os.getenv("CLIPWRIGHT_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser() / FILE_NAME
    data_dir = (os.getenv("CLIPWRIGHT_DATA_DIR") or "").strip()
    root = Path(data_dir).expanduser() if data_dir else Path("data")
    return root / "config" / FILE_NAME


def _read_file(path: Path) -> Optional[bool]:
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable offline-mode override at %s: %s", path, exc)
        return None
    value = payload.get("offline_mode") if isinstance(payload, dict) else None
    if isinstance(value, bool):
        globals()["_cache"] = (str(path), stat.st_mtime_ns, stat.st_size, value)
        return value
    logger.warning("Ignoring offline-mode override without a boolean: %s", path)
    return None


def read_override() -> Optional[bool]:
    """The persisted runtime value, or ``None`` when it has never been set."""
    path = state_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    cached_path, cached_mtime, cached_size, cached_value = _cache
    if (
        cached_path == str(path)
        and cached_mtime == stat.st_mtime_ns
        and cached_size == stat.st_size
    ):
        return cached_value
    return _read_file(path)


def resolve(env_value: bool) -> tuple[bool, str]:
    """Combine the ``.env`` default with any runtime override.

    Returns ``(offline_mode, source)`` where source is ``"runtime"`` or
    ``"env"`` so the API and the UI can say which one is winning.
    """
    override = read_override()
    if override is None:
        return bool(env_value), "env"
    return override, "runtime"


def write_override(value: Optional[bool]) -> Optional[bool]:
    """Persist (or clear) the runtime value. Returns the value now stored."""
    path = state_path()
    if value is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("Could not clear offline-mode override at %s: %s", path, exc)
            raise
        globals()["_cache"] = (None, None, None, None)
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"offline_mode": bool(value)}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("Could not persist offline-mode override at %s: %s", path, exc)
        raise
    globals()["_cache"] = (None, None, None, None)
    logger.info("Offline mode runtime override set to %s", bool(value))
    return bool(value)


def reset_cache() -> None:
    """Drop the mtime cache (tests and long-lived processes)."""
    globals()["_cache"] = (None, None, None, None)
