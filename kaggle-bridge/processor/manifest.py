"""Job/result manifests for the ClipWright Kaggle bridge.

This module is deliberately dependency-free (stdlib only) so it can run inside
the Kaggle kernel, inside the tests, and on a Windows dev box without installing
torch or ffmpeg.

Two documents are defined:

``clipwright.kaggle.job/1``
    Produced by the operator (or, in Phase 2, by the Oracle backend). Describes
    one video to process and exactly how to process it.

``clipwright.kaggle.result/1``
    Produced by the kernel. Describes what actually happened and where every
    artefact lives.

Rules enforced here:

* No credentials of any kind. Keys that look like secrets are rejected, and
  string values are scanned for bearer/basic auth prefixes.
* Checksums are SHA-256, lowercase hex.
* Every output file recorded in a result must also carry its size and checksum,
  so the receiver can verify the transfer without trusting the transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

JOB_SCHEMA_VERSION = "clipwright.kaggle.job/1"
RESULT_SCHEMA_VERSION = "clipwright.kaggle.result/1"

SOURCE_TYPES = ("local_file", "youtube")
PROCESSING_MODES = ("gpu", "cpu")
RENDER_CODECS = ("libx264", "h264_nvenc")

#: Keys that must never appear anywhere in a manifest. Checked case-insensitively
#: and as substrings, because ``kaggle_key`` / ``hf_token`` / ``bridge_token``
#: are all variations of the same mistake.
FORBIDDEN_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "credential",
    "private_key",
    "access_key",
)

#: Keys that end in these suffixes are credentials whatever the prefix is
#: (``kaggle_key``, ``hf_key``, ``bridge_key``). ``key_topics`` is a legitimate
#: analysis field and is not caught by a suffix rule.
FORBIDDEN_KEY_SUFFIXES = ("_key", "_keys", "_token", "_secret", "_password")

#: Values that indicate someone pasted a header or a signed URL into the job.
FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^\s*basic\s+\S+", re.IGNORECASE),
    re.compile(r"x-amz-signature", re.IGNORECASE),
    re.compile(r"x-goog-signature", re.IGNORECASE),
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid or unsafe to run."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "invalid manifest")


@dataclass
class ValidationOutcome:
    """Result of a non-raising manifest check."""

    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise ManifestError(self.errors)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def sha256_file(path: os.PathLike[str] | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the lowercase hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: os.PathLike[str] | str) -> Dict[str, Any]:
    """Parse a job/result manifest.

    ``utf-8-sig`` so a file written by Windows PowerShell 5.1 (whose
    ``Set-Content -Encoding UTF8`` emits a BOM) still loads; ``json`` refuses a
    BOM under plain ``utf-8``.
    """
    with open(path, "r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def dump_json(path: os.PathLike[str] | str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=False, ensure_ascii=False)
        stream.write("\n")
    os.replace(temporary, destination)


def _is_secret_key(raw_key: str) -> bool:
    """True when a JSON key names a credential.

    Normalises ``X-Api-Key`` and ``kaggle_key`` to the same shape before
    matching, so a header pasted into a manifest cannot slip through, while
    ``key_topics`` stays allowed.
    """
    normalised = re.sub(r"[^a-z0-9]+", "_", str(raw_key).lower()).strip("_")
    if not normalised:
        return False
    if normalised == "key" or normalised.endswith(FORBIDDEN_KEY_SUFFIXES):
        return True
    return any(fragment in normalised for fragment in FORBIDDEN_KEY_FRAGMENTS)


def _scan_for_secrets(node: Any, path: str, outcome: ValidationOutcome) -> None:
    """Walk the document and flag credential-shaped keys and values."""
    if isinstance(node, Mapping):
        for raw_key, value in node.items():
            key = str(raw_key)
            if _is_secret_key(key):
                outcome.errors.append(f"{path}.{key}: credential-shaped key is not allowed")
            _scan_for_secrets(value, f"{path}.{key}", outcome)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan_for_secrets(item, f"{path}[{index}]", outcome)
    elif isinstance(node, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(node):
                outcome.errors.append(f"{path}: value looks like a credential or signed URL")
                break


def _require(mapping: Mapping[str, Any], key: str, path: str, outcome: ValidationOutcome) -> Any:
    if key not in mapping:
        outcome.errors.append(f"{path}.{key}: missing required field")
        return None
    return mapping[key]


def _require_str(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    outcome: ValidationOutcome,
    *,
    allow_empty: bool = False,
    choices: Optional[Iterable[str]] = None,
) -> Optional[str]:
    value = _require(mapping, key, path, outcome)
    if value is None:
        return None
    if not isinstance(value, str):
        outcome.errors.append(f"{path}.{key}: expected a string, got {type(value).__name__}")
        return None
    if not value.strip() and not allow_empty:
        outcome.errors.append(f"{path}.{key}: must not be empty")
        return None
    if choices is not None and value not in tuple(choices):
        outcome.errors.append(f"{path}.{key}: '{value}' is not one of {tuple(choices)}")
    return value


def _require_number(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    outcome: ValidationOutcome,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Optional[float]:
    value = _require(mapping, key, path, outcome)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        outcome.errors.append(f"{path}.{key}: expected a number, got {type(value).__name__}")
        return None
    if minimum is not None and value < minimum:
        outcome.errors.append(f"{path}.{key}: {value} is below the minimum {minimum}")
    if maximum is not None and value > maximum:
        outcome.errors.append(f"{path}.{key}: {value} is above the maximum {maximum}")
    return float(value)


def _require_bool(
    mapping: Mapping[str, Any], key: str, path: str, outcome: ValidationOutcome, *, default: Optional[bool] = None
) -> Optional[bool]:
    if key not in mapping:
        if default is None:
            outcome.errors.append(f"{path}.{key}: missing required field")
            return None
        return default
    value = mapping[key]
    if not isinstance(value, bool):
        outcome.errors.append(f"{path}.{key}: expected a boolean, got {type(value).__name__}")
        return None
    return value


def _require_mapping(
    mapping: Mapping[str, Any], key: str, path: str, outcome: ValidationOutcome
) -> Optional[Mapping[str, Any]]:
    value = _require(mapping, key, path, outcome)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        outcome.errors.append(f"{path}.{key}: expected an object")
        return None
    return value


def _require_list(
    mapping: Mapping[str, Any], key: str, path: str, outcome: ValidationOutcome
) -> Optional[Sequence[Any]]:
    value = _require(mapping, key, path, outcome)
    if value is None:
        return None
    if not isinstance(value, list):
        outcome.errors.append(f"{path}.{key}: expected an array")
        return None
    return value


# --------------------------------------------------------------------------- #
# job manifest
# --------------------------------------------------------------------------- #


def validate_job(job: Mapping[str, Any]) -> ValidationOutcome:
    """Validate a job manifest without raising.

    Returns every problem found, so the operator sees all of them at once
    instead of fixing one per push cycle.
    """
    outcome = ValidationOutcome()

    if not isinstance(job, Mapping):
        outcome.errors.append("job: expected a JSON object")
        return outcome

    _scan_for_secrets(job, "job", outcome)

    version = _require_str(job, "schema_version", "job", outcome)
    if version is not None and version != JOB_SCHEMA_VERSION:
        outcome.errors.append(
            f"job.schema_version: expected {JOB_SCHEMA_VERSION!r}, got {version!r}"
        )

    _require_str(job, "job_id", "job", outcome)

    source = _require_mapping(job, "source", "job", outcome)
    if source is not None:
        source_type = _require_str(
            source, "type", "job.source", outcome, choices=SOURCE_TYPES
        )
        _require_str(source, "filename", "job.source", outcome)
        url = source.get("url")
        if source_type == "youtube":
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                outcome.errors.append(
                    "job.source.url: required for source type 'youtube' and must be an http(s) URL"
                )
        elif url is not None:
            outcome.warnings.append(
                "job.source.url is set but the source type is 'local_file'; it will be ignored"
            )
        expected_sha = source.get("sha256")
        if expected_sha is not None:
            if not isinstance(expected_sha, str) or not SHA256_RE.match(expected_sha):
                outcome.errors.append("job.source.sha256: expected 64 lowercase hex characters")
        expected_size = source.get("size_bytes")
        if expected_size is not None:
            _require_number(source, "size_bytes", "job.source", outcome, minimum=1)

    processing = _require_mapping(job, "processing", "job", outcome)
    if processing is not None:
        _require_str(
            processing, "mode", "job.processing", outcome, choices=PROCESSING_MODES
        )
        require_gpu = _require_bool(
            processing, "require_gpu", "job.processing", outcome, default=True
        )
        if processing.get("mode") == "gpu" and require_gpu is False:
            outcome.warnings.append(
                "job.processing: mode is 'gpu' but require_gpu is false; CPU fallback will be allowed"
            )

    transcription = _require_mapping(job, "transcription", "job", outcome)
    if transcription is not None:
        _require_str(transcription, "model", "job.transcription", outcome)
        _require_str(
            transcription,
            "device",
            "job.transcription",
            outcome,
            choices=("auto", "cuda", "cpu"),
        )
        _require_bool(
            transcription, "word_timestamps", "job.transcription", outcome, default=True
        )
        language = transcription.get("language")
        if language is not None and not isinstance(language, str):
            outcome.errors.append("job.transcription.language: expected a string or null")

    ai = _require_mapping(job, "ai", "job", outcome)
    if ai is not None:
        _require_bool(ai, "enabled", "job.ai", outcome, default=True)
        _require_str(ai, "model", "job.ai", outcome)
        backend = ai.get("backend")
        if backend is not None and backend not in ("ollama", "transformers", "auto"):
            outcome.errors.append(
                "job.ai.backend: expected 'ollama', 'transformers' or 'auto'"
            )
        max_segments = ai.get("max_segments")
        if max_segments is not None:
            _require_number(ai, "max_segments", "job.ai", outcome, minimum=1, maximum=20)

    rendering = _require_mapping(job, "rendering", "job", outcome)
    if rendering is not None:
        codec = _require_str(
            rendering, "codec", "job.rendering", outcome, choices=RENDER_CODECS
        )
        if codec == "h264_nvenc":
            outcome.warnings.append(
                "job.rendering.codec is h264_nvenc; the kernel falls back to libx264 if NVENC is unavailable"
            )
        target = _require_mapping(rendering, "target", "job.rendering", outcome)
        if target is not None:
            width = _require_number(target, "width", "job.rendering.target", outcome, minimum=16)
            height = _require_number(target, "height", "job.rendering.target", outcome, minimum=16)
            if width is not None and int(width) % 2:
                outcome.errors.append("job.rendering.target.width: must be even for yuv420p")
            if height is not None and int(height) % 2:
                outcome.errors.append("job.rendering.target.height: must be even for yuv420p")
            fps = _require_number(target, "fps", "job.rendering.target", outcome, minimum=1, maximum=120)
            if fps is not None and float(fps) not in (23.976, 24, 25, 29.97, 30, 50, 59.94, 60):
                outcome.warnings.append(f"job.rendering.target.fps: unusual frame rate {fps}")
        _require_number(rendering, "crf", "job.rendering", outcome, minimum=0, maximum=51)
        _require_str(rendering, "preset", "job.rendering", outcome)
        captions = rendering.get("captions")
        if captions is not None and not isinstance(captions, bool):
            outcome.errors.append("job.rendering.captions: expected a boolean")

    outputs = _require_mapping(job, "outputs", "job", outcome)
    if outputs is not None:
        _require_bool(outputs, "write_transcript", "job.outputs", outcome, default=True)
        _require_bool(outputs, "write_ai_selection", "job.outputs", outcome, default=True)
        _require_bool(outputs, "write_reports", "job.outputs", outcome, default=True)

    _require_bool(job, "dry_run", "job", outcome, default=False)

    if not outcome.errors:
        source = job.get("source") or {}
        if source.get("type") == "local_file":
            filename = source.get("filename")
            if isinstance(filename, str) and not _safe_relative_name(filename):
                outcome.errors.append(
                    "job.source.filename: must be a bare file name without path separators"
                )
    return outcome


def _safe_relative_name(value: str) -> bool:
    return (
        "/" not in value
        and "\\" not in value
        and value not in (".", "..")
        and not value.startswith("~")
    )


def load_and_validate_job(path: os.PathLike[str] | str) -> Dict[str, Any]:
    job = load_json(path)
    validate_job(job).raise_if_invalid()
    return job


# --------------------------------------------------------------------------- #
# result manifest
# --------------------------------------------------------------------------- #


@dataclass
class OutputFile:
    """One produced artefact, verified on disk."""

    name: str
    path: str
    size_bytes: int
    sha256: str

    @classmethod
    def from_path(cls, path: os.PathLike[str] | str, *, name: Optional[str] = None) -> "OutputFile":
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"output file is missing: {target}")
        return cls(
            name=name or target.name,
            path=str(target),
            size_bytes=target.stat().st_size,
            sha256=sha256_file(target),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def build_result(
    *,
    job: Mapping[str, Any],
    status: str,
    outputs: Sequence[OutputFile],
    transcript_path: Optional[str] = None,
    ai_selection: Optional[Mapping[str, Any]] = None,
    durations_seconds: Optional[Mapping[str, float]] = None,
    validation_report: Optional[Mapping[str, Any]] = None,
    error: Optional[Mapping[str, Any]] = None,
    environment: Optional[Mapping[str, Any]] = None,
    notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Assemble a result manifest.

    ``status`` is one of ``completed``, ``failed``, ``partial``. A failed run
    still produces a manifest: knowing *why* it failed is the point.
    """
    if status not in ("completed", "failed", "partial"):
        raise ValueError(f"unsupported status: {status!r}")

    payload: Dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job_id": job.get("job_id"),
        "status": status,
        "produced_at": utc_now_iso(),
        "job_schema_version": job.get("schema_version"),
        "outputs": [item.as_dict() for item in outputs],
        "files": [item.as_dict() for item in outputs],
        "transcript": {"path": transcript_path} if transcript_path else None,
        "ai_selection": dict(ai_selection) if ai_selection else None,
        "durations_seconds": dict(durations_seconds or {}),
        "validation": dict(validation_report or {}),
        "environment": dict(environment or {}),
        "error": dict(error) if error else None,
        "notes": list(notes or []),
    }
    return payload


RESULT_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "status",
    "produced_at",
    "outputs",
)


def validate_result(result: Mapping[str, Any]) -> ValidationOutcome:
    """Validate a result manifest, including every recorded checksum shape."""
    outcome = ValidationOutcome()
    if not isinstance(result, Mapping):
        outcome.errors.append("result: expected a JSON object")
        return outcome

    _scan_for_secrets(result, "result", outcome)

    for key in RESULT_REQUIRED_FIELDS:
        _require(result, key, "result", outcome)

    if result.get("schema_version") not in (None, RESULT_SCHEMA_VERSION):
        outcome.errors.append(
            f"result.schema_version: expected {RESULT_SCHEMA_VERSION!r}"
        )

    status = result.get("status")
    if status is not None and status not in ("completed", "failed", "partial"):
        outcome.errors.append(f"result.status: unsupported value {status!r}")

    if status == "failed" and not result.get("error"):
        outcome.errors.append("result.error: required when status is 'failed'")

    outputs = result.get("outputs")
    if isinstance(outputs, list):
        for index, item in enumerate(outputs):
            path = f"result.outputs[{index}]"
            if not isinstance(item, Mapping):
                outcome.errors.append(f"{path}: expected an object")
                continue
            _require_str(item, "name", path, outcome)
            _require_str(item, "path", path, outcome)
            size = _require_number(item, "size_bytes", path, outcome, minimum=0)
            if size is not None and size <= 0:
                outcome.errors.append(f"{path}.size_bytes: output is empty")
            digest = _require_str(item, "sha256", path, outcome)
            if digest is not None and not SHA256_RE.match(digest):
                outcome.errors.append(f"{path}.sha256: expected 64 lowercase hex characters")
    elif outputs is not None:
        outcome.errors.append("result.outputs: expected an array")

    durations = result.get("durations_seconds")
    if durations is not None:
        if not isinstance(durations, Mapping):
            outcome.errors.append("result.durations_seconds: expected an object")
        else:
            for key, value in durations.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    outcome.errors.append(
                        f"result.durations_seconds.{key}: expected a non-negative number"
                    )
    return outcome


def load_and_validate_result(path: os.PathLike[str] | str) -> Dict[str, Any]:
    result = load_json(path)
    validate_result(result).raise_if_invalid()
    return result


def verify_outputs(result: Mapping[str, Any], *, base_dir: Optional[os.PathLike[str] | str] = None) -> ValidationOutcome:
    """Re-check every output on disk against the checksums in a result manifest.

    Used by whoever receives the artefacts (Phase 2: the Oracle backend) and by
    the kernel's own self-check before it declares success.
    """
    outcome = ValidationOutcome()
    root = Path(base_dir) if base_dir else None
    for index, item in enumerate(result.get("outputs") or []):
        if not isinstance(item, Mapping):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        target = Path(raw_path)
        if not target.is_absolute() and root is not None:
            target = root / target
        if not target.is_file():
            outcome.errors.append(f"outputs[{index}]: missing file {target}")
            continue
        size = target.stat().st_size
        if isinstance(item.get("size_bytes"), int) and size != item["size_bytes"]:
            outcome.errors.append(
                f"outputs[{index}]: size mismatch for {target.name} ({size} != {item['size_bytes']})"
            )
        expected = item.get("sha256")
        if isinstance(expected, str):
            actual = sha256_file(target)
            if actual != expected:
                outcome.errors.append(
                    f"outputs[{index}]: checksum mismatch for {target.name}"
                )
    return outcome
