"""Gate B — Whisper transcription, matching ClipWright's cache format exactly.

Production references (``backend/src/media/transcription.py`` @ ``a73607d``):

* ``_prepare_audio_for_transcription`` → 16 kHz mono, 64 kbps MP3 first, so a
  90-minute source never has to be decoded at full size. Mirrored in
  :func:`extract_transcription_audio`.
* ``_get_whisper_model`` → ``whisper.load_model(name, device=..., download_root=...)``
  and a *refusal to download during a job*. Mirrored, with ``allow_download``.
* ``transcribe_with_whisper`` → ``model.transcribe(path, word_timestamps=True, language=..., verbose=None)``.
* ``_whisper_result_to_transcript_data`` → the ``version: 2`` cache with
  millisecond timings, mirrored in :func:`to_transcript_cache`.

Package collision
-----------------
``backend/pyproject.toml`` depends on **both** ``openai-whisper`` and the legacy
``whisper`` distribution, and both install a top-level ``whisper`` module. Which
one imports is decided by installation order, not by intent. This module never
installs anything; it *reports* what actually loaded (``whisper.__file__`` plus
the distributions that claim to provide the module) and refuses to run when the
loaded module is not openai-whisper, because the legacy package has no
``load_model`` with the same semantics.

The bridge's own ``requirements.txt`` installs ``openai-whisper`` only, so the
prototype environment cannot reproduce the collision — the check exists to prove
that on Kaggle, and to document the production bug.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

TRANSCRIPT_CACHE_SCHEMA_VERSION = 2

#: Mirror of the production audio prep settings.
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_BITRATE = "64k"


class TranscriptionError(RuntimeError):
    """Raised when Whisper cannot be loaded, or the source cannot be transcribed."""


@dataclass
class TranscriptionResult:
    transcript: Dict[str, Any]
    model: str
    device: str
    language: Optional[str]
    duration_seconds: float
    audio_seconds: float
    real_time_factor: float
    segment_count: int
    word_count: int
    audio_path: str
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "device": self.device,
            "language": self.language,
            "duration_seconds": round(self.duration_seconds, 3),
            "audio_seconds": round(self.audio_seconds, 3),
            "real_time_factor": round(self.real_time_factor, 4),
            "segment_count": self.segment_count,
            "word_count": self.word_count,
            "audio_path": self.audio_path,
            "warnings": list(self.warnings),
            "transcript_schema_version": self.transcript.get("version"),
        }


# --------------------------------------------------------------------------- #
# environment / collision diagnostics
# --------------------------------------------------------------------------- #


def whisper_environment() -> Dict[str, Any]:
    """Describe the Whisper installation without importing torch.whisper twice."""
    info: Dict[str, Any] = {
        "importable": False,
        "module_path": None,
        "module_version": None,
        "distributions": [],
        "collision": False,
        "collision_detail": None,
        "available_models": [],
    }

    try:
        from importlib import metadata

        packages = metadata.packages_distributions()
        providers = list(packages.get("whisper", []))
        info["distributions"] = providers
        info["collision"] = len(providers) > 1
        if info["collision"]:
            info["collision_detail"] = (
                "multiple distributions provide the top-level 'whisper' module: "
                + ", ".join(sorted(providers))
            )
    except Exception:  # pragma: no cover - metadata is best effort
        pass

    try:
        import whisper  # type: ignore

        info["importable"] = True
        info["module_path"] = getattr(whisper, "__file__", None)
        info["module_version"] = getattr(whisper, "__version__", None)
        info["available_models"] = list(getattr(whisper, "available_models", lambda: [])())
        info["has_load_model"] = hasattr(whisper, "load_model")
    except Exception as error:  # pragma: no cover - only on broken installs
        info["import_error"] = f"{type(error).__name__}: {error}"

    return info


def assert_openai_whisper() -> Dict[str, Any]:
    """Fail loudly when the legacy ``whisper`` distribution is the one loaded."""
    info = whisper_environment()
    if not info["importable"]:
        raise TranscriptionError(
            "Whisper is not importable in this environment: "
            f"{info.get('import_error', 'unknown import error')}. "
            "Install it with: pip install openai-whisper"
        )
    if not info.get("has_load_model"):
        raise TranscriptionError(
            "the imported 'whisper' module has no load_model(); this is the legacy "
            f"'whisper' distribution, not openai-whisper (module at {info['module_path']}). "
            "Uninstall the legacy package and install openai-whisper."
        )
    return info


# --------------------------------------------------------------------------- #
# audio extraction
# --------------------------------------------------------------------------- #


def extract_transcription_audio(video_path: Path, output_path: Optional[Path] = None) -> Path:
    """Extract a 16 kHz mono 64 kbps MP3, exactly like production does."""
    video_path = Path(video_path)
    audio_path = Path(output_path) if output_path else video_path.with_name(
        f"{video_path.stem}.transcription.mp3"
    )
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return audio_path

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        str(AUDIO_CHANNELS),
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-b:a",
        AUDIO_BITRATE,
        str(audio_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
        raise TranscriptionError(
            "ffmpeg could not extract audio for transcription: "
            + (result.stderr or "").strip().splitlines()[-1:][0][:300]
        )
    return audio_path


def audio_duration_seconds(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# model + transcription
# --------------------------------------------------------------------------- #


def resolve_device(requested: str = "auto") -> str:
    """Resolve ``auto`` to cuda when torch can actually use it."""
    if requested in ("cuda", "cpu"):
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # pragma: no cover
        return "cpu"


def load_model(
    model_size: str = "base",
    *,
    device: str = "auto",
    download_root: Optional[str] = None,
    allow_download: bool = True,
):
    """Load a Whisper checkpoint, refusing to silently fetch weights when asked not to."""
    assert_openai_whisper()
    import whisper  # type: ignore

    resolved_device = resolve_device(device)

    if download_root and not allow_download:
        expected = Path(download_root) / f"{model_size}.pt"
        if not expected.exists():
            raise TranscriptionError(
                f"Whisper checkpoint {expected} is missing and downloads are disabled for this job"
            )

    if download_root:
        Path(download_root).mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    model = whisper.load_model(model_size, device=resolved_device, download_root=download_root)
    load_seconds = time.perf_counter() - started
    setattr(model, "_clipwright_load_seconds", load_seconds)
    return model


def transcribe(
    video_path: Path,
    *,
    model_size: str = "base",
    device: str = "auto",
    language: Optional[str] = None,
    download_root: Optional[str] = None,
    allow_download: bool = True,
    word_timestamps: bool = True,
) -> TranscriptionResult:
    """Transcribe a video and convert the result to the ClipWright cache format."""
    video_path = Path(video_path)
    if not video_path.is_file():
        raise TranscriptionError(f"source video not found: {video_path}")

    warnings: List[str] = []
    audio_path = extract_transcription_audio(video_path)
    audio_seconds = audio_duration_seconds(audio_path)
    if audio_path == video_path:
        warnings.append("audio extraction was skipped; Whisper will decode the video directly")

    model = load_model(
        model_size, device=device, download_root=download_root, allow_download=allow_download
    )
    resolved_device = resolve_device(device)
    load_seconds = float(getattr(model, "_clipwright_load_seconds", 0.0))

    # Production reduces regional codes such as en_us to the ISO base Whisper expects.
    whisper_language = language.split("_", 1)[0] if language else None

    started = time.perf_counter()
    raw = model.transcribe(
        str(audio_path),
        word_timestamps=word_timestamps,
        language=whisper_language,
        verbose=None,
    )
    elapsed = time.perf_counter() - started

    transcript = to_transcript_cache(raw)
    if not transcript["utterances"]:
        warnings.append("Whisper returned no segments; the transcript is empty")

    rtf = (audio_seconds / elapsed) if elapsed > 0 and audio_seconds > 0 else 0.0
    return TranscriptionResult(
        transcript=transcript,
        model=model_size,
        device=resolved_device,
        language=raw.get("language") if isinstance(raw, Mapping) else None,
        duration_seconds=elapsed,
        audio_seconds=audio_seconds,
        real_time_factor=rtf,
        segment_count=len(transcript["utterances"]),
        word_count=len(transcript["words"]),
        audio_path=str(audio_path),
        warnings=warnings,
    )


def to_transcript_cache(whisper_result: Mapping[str, Any]) -> Dict[str, Any]:
    """Port of ``_whisper_result_to_transcript_data``.

    Whisper reports seconds; the cache stores milliseconds. Keys and defaults
    are identical to production so downstream analysis code cannot tell the
    difference between a local and a Kaggle transcript.
    """
    words_data: List[Dict[str, Any]] = []
    utterances_data: List[Dict[str, Any]] = []

    for segment in whisper_result.get("segments") or []:
        seg_words = [
            {
                "text": word.get("word", word.get("text", "")),
                "start": int(word["start"] * 1000) if isinstance(word.get("start"), float) else int(word.get("start", 0)),
                "end": int(word["end"] * 1000) if isinstance(word.get("end"), float) else int(word.get("end", 0)),
                "confidence": word.get("probability", word.get("confidence", 1.0)),
                "speaker": None,
            }
            for word in segment.get("words") or []
        ]
        utterances_data.append(
            {
                "text": segment.get("text", ""),
                "start": int(segment["start"] * 1000) if "start" in segment else 0,
                "end": int(segment["end"] * 1000) if "end" in segment else 0,
                "speaker": None,
                "words": seg_words,
            }
        )
        words_data.extend(seg_words)

    return {
        "version": TRANSCRIPT_CACHE_SCHEMA_VERSION,
        "words": words_data,
        "utterances": utterances_data,
        "text": whisper_result.get("text", ""),
        "language_code": whisper_result.get("language"),
    }


def write_transcript(path: Path, transcript: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(transcript, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
    return path
