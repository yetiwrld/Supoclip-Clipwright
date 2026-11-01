"""Output validation for the ClipWright Kaggle bridge.

Two layers, on purpose:

* **Pure checks** (:func:`compare_duration`, :func:`fps_matches`, …) take plain
  numbers and return structured results. They are what the unit tests exercise
  and they need neither ffmpeg nor a GPU.
* **Media checks** (:func:`probe_media`, :func:`decode_check`, :func:`run_output_checks`)
  shell out to ffprobe/ffmpeg and produce the validation report embedded in the
  result manifest.

The Gate G acceptance list is implemented verbatim: the file opens, it has
audio that is not silent, audio and video stay in sync, the duration matches
what was asked for, the frame rate and resolution match the render target, and
the whole file decodes without error.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

DEFAULT_DURATION_TOLERANCE_SECONDS = 0.75
DEFAULT_FPS_RELATIVE_TOLERANCE = 0.01
DEFAULT_AV_SYNC_TOLERANCE_MS = 120
DEFAULT_SILENCE_FLOOR_DB = -60.0


class MediaToolError(RuntimeError):
    """Raised when ffmpeg/ffprobe is missing or cannot read a file."""


@dataclass
class Check:
    """One named pass/fail with the measured value attached."""

    name: str
    ok: bool
    detail: str
    severity: str = "error"  # "error" fails the run, "warning" is recorded only

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "severity": self.severity}


@dataclass
class ValidationReport:
    checks: List[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def extend(self, checks: Iterable[Check]) -> None:
        self.checks.extend(checks)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.severity == "error")

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.severity != "error"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": len(self.checks),
            "failed": [c.as_dict() for c in self.failures],
            "warnings": [c.as_dict() for c in self.warnings],
            "checks": [c.as_dict() for c in self.checks],
        }


# --------------------------------------------------------------------------- #
# pure checks
# --------------------------------------------------------------------------- #


def duration_tolerance(expected_seconds: float, *, minimum: float = DEFAULT_DURATION_TOLERANCE_SECONDS) -> float:
    """Tolerance for a clip duration.

    Frame-boundary rounding means a 30.0 s clip can legitimately land at
    29.97 s; the floor covers that, the percentage covers longer clips.
    """
    return max(minimum, abs(expected_seconds) * 0.01)


def compare_duration(
    expected_seconds: float,
    actual_seconds: float,
    *,
    tolerance: Optional[float] = None,
) -> Check:
    if expected_seconds <= 0:
        return Check("duration", False, f"expected duration is not positive: {expected_seconds}")
    allowed = tolerance if tolerance is not None else duration_tolerance(expected_seconds)
    delta = abs(actual_seconds - expected_seconds)
    return Check(
        "duration",
        delta <= allowed,
        f"expected {expected_seconds:.3f}s, measured {actual_seconds:.3f}s, delta {delta:.3f}s (allowed {allowed:.3f}s)",
    )


def fps_matches(
    actual_fps: float,
    expected_fps: float,
    *,
    relative_tolerance: float = DEFAULT_FPS_RELATIVE_TOLERANCE,
) -> Check:
    if expected_fps <= 0:
        return Check("fps", False, f"expected fps is not positive: {expected_fps}")
    allowed = expected_fps * relative_tolerance
    delta = abs(actual_fps - expected_fps)
    return Check(
        "fps",
        delta <= allowed,
        f"expected {expected_fps:g} fps, measured {actual_fps:.3f} fps, delta {delta:.3f} (allowed {allowed:.3f})",
    )


def resolution_matches(actual: tuple[int, int], expected: tuple[int, int]) -> Check:
    ok = tuple(int(v) for v in actual) == tuple(int(v) for v in expected)
    return Check(
        "resolution",
        ok,
        f"expected {expected[0]}x{expected[1]}, measured {actual[0]}x{actual[1]}",
    )


def av_sync_check(
    video_duration: float,
    audio_duration: float,
    *,
    tolerance_ms: int = DEFAULT_AV_SYNC_TOLERANCE_MS,
) -> Check:
    """Compare the video and audio stream durations of the rendered file."""
    drift_ms = abs(video_duration - audio_duration) * 1000.0
    return Check(
        "av_sync",
        drift_ms <= tolerance_ms,
        f"video {video_duration:.3f}s vs audio {audio_duration:.3f}s → drift {drift_ms:.0f}ms (allowed {tolerance_ms}ms)",
    )


def frames_expected(duration_seconds: float, fps: float) -> int:
    """Nominal frame count for a CFR segment (used to sanity-check frame pacing)."""
    return int(round(duration_seconds * fps))


def frame_pacing_check(frame_count: int, duration_seconds: float, fps: float) -> Check:
    """A CFR file should hold roughly duration × fps frames."""
    if duration_seconds <= 0 or fps <= 0:
        return Check("frame_pacing", False, "duration and fps must be positive")
    expected = frames_expected(duration_seconds, fps)
    allowed = max(2, int(expected * 0.02))
    delta = abs(frame_count - expected)
    return Check(
        "frame_pacing",
        delta <= allowed,
        f"{frame_count} frames for {duration_seconds:.2f}s @ {fps:g}fps (expected ~{expected}, allowed ±{allowed})",
    )


def silence_check(mean_volume_db: Optional[float], *, floor_db: float = DEFAULT_SILENCE_FLOOR_DB) -> Check:
    """Audio must exist and must not be digital silence."""
    if mean_volume_db is None:
        return Check("audio_present", False, "no mean volume could be measured")
    ok = mean_volume_db > floor_db
    return Check(
        "audio_present",
        ok,
        f"mean volume {mean_volume_db:.1f} dB (floor {floor_db} dB)",
    )


def parse_fraction(value: Any) -> Optional[float]:
    """Parse ffprobe's ``"30000/1001"`` style rates without floating drift."""
    if value is None:
        return None
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------- #
# media checks (ffprobe / ffmpeg)
# --------------------------------------------------------------------------- #


def _require_binary(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise MediaToolError(f"{name} is not installed or not on PATH")
    return resolved


def probe_media(path: Path) -> Dict[str, Any]:
    """Return ffprobe's JSON for a file."""
    ffprobe = _require_binary("ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise MediaToolError(f"ffprobe failed for {path.name}: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout or "{}")


def stream_summary(probe: Mapping[str, Any]) -> Dict[str, Any]:
    """Reduce an ffprobe payload to the fields the bridge cares about."""
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subtitle = next((s for s in streams if s.get("codec_type") == "subtitle"), None)
    fmt = probe.get("format") or {}

    summary: Dict[str, Any] = {
        "container_duration": _to_float(fmt.get("duration")),
        "format_name": fmt.get("format_name"),
        "bit_rate": _to_float(fmt.get("bit_rate")),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "has_subtitle_stream": subtitle is not None,
        "video": None,
        "audio": None,
    }
    if video is not None:
        summary["video"] = {
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": parse_fraction(video.get("avg_frame_rate")) or parse_fraction(video.get("r_frame_rate")),
            "duration": _to_float(video.get("duration")) or _to_float(fmt.get("duration")),
            "nb_frames": _to_int(video.get("nb_frames")),
            "pix_fmt": video.get("pix_fmt"),
        }
    if audio is not None:
        summary["audio"] = {
            "codec": audio.get("codec_name"),
            "sample_rate": _to_int(audio.get("sample_rate")),
            "channels": _to_int(audio.get("channels")),
            "duration": _to_float(audio.get("duration")) or _to_float(fmt.get("duration")),
        }
    return summary


def decode_check(path: Path) -> Check:
    """Decode every frame to /dev/null; any reported error means corruption."""
    ffmpeg = _require_binary("ffmpeg")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return Check("decodes_cleanly", False, "decode check timed out")
    stderr = (result.stderr or "").strip()
    ok = result.returncode == 0 and not stderr
    detail = "clean decode" if ok else (stderr.splitlines()[0][:300] if stderr else f"exit {result.returncode}")
    return Check("decodes_cleanly", ok, detail)


def measure_mean_volume(path: Path) -> Optional[float]:
    """Return the mean volume in dBFS from ffmpeg's volumedetect filter."""
    ffmpeg = _require_binary("ffmpeg")
    command = [ffmpeg, "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    for line in (result.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                return None
    return None


def count_frames(path: Path) -> Optional[int]:
    """Count decoded video frames (exact, but reads the whole file)."""
    ffprobe = _require_binary("ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    value = (result.stdout or "").strip().split(",")[0]
    return _to_int(value)


def run_output_checks(
    path: Path,
    *,
    expected_duration: Optional[float] = None,
    expected_size: Optional[tuple[int, int]] = None,
    expected_fps: Optional[float] = None,
    expect_audio: bool = True,
    check_decode: bool = True,
) -> ValidationReport:
    """Full Gate G check for one rendered clip."""
    report = ValidationReport()

    if not path.is_file():
        report.add(Check("file_exists", False, f"missing file: {path}"))
        return report
    if path.stat().st_size == 0:
        report.add(Check("file_not_empty", False, "file is 0 bytes"))
        return report
    report.add(Check("file_not_empty", True, f"{path.stat().st_size} bytes"))

    try:
        summary = stream_summary(probe_media(path))
    except MediaToolError as error:
        report.add(Check("opens", False, str(error)))
        return report

    report.add(Check("opens", summary["has_video"], f"container={summary['format_name']}"))

    video = summary.get("video") or {}
    audio = summary.get("audio") or {}

    if expected_size is not None and video:
        report.add(resolution_matches((video["width"], video["height"]), expected_size))

    if expected_fps is not None and video.get("fps"):
        report.add(fps_matches(float(video["fps"]), float(expected_fps)))

    measured_duration = summary.get("container_duration") or video.get("duration") or 0.0
    if expected_duration is not None:
        report.add(compare_duration(float(expected_duration), float(measured_duration)))

    if expect_audio:
        if not summary["has_audio"]:
            report.add(Check("audio_present", False, "no audio stream in the output"))
        else:
            report.add(Check("audio_present", True, f"{audio.get('codec')} {audio.get('sample_rate')}Hz {audio.get('channels')}ch"))
            video_duration = video.get("duration") or measured_duration
            audio_duration = audio.get("duration") or measured_duration
            report.add(av_sync_check(float(video_duration), float(audio_duration)))
            report.add(silence_check(measure_mean_volume(path)))

    if check_decode:
        report.add(decode_check(path))

    frames = count_frames(path)
    if frames is not None and video.get("fps") and measured_duration:
        report.add(frame_pacing_check(frames, float(measured_duration), float(video["fps"])))

    return report


def run_source_checks(path: Path) -> ValidationReport:
    """Gate G steps 1–2: the input file exists, probes cleanly and has audio."""
    report = ValidationReport()
    if not path.is_file():
        report.add(Check("source_exists", False, f"missing source: {path}"))
        return report
    report.add(Check("source_exists", True, f"{path.stat().st_size} bytes"))

    try:
        summary = stream_summary(probe_media(path))
    except MediaToolError as error:
        report.add(Check("source_probes", False, str(error)))
        return report

    report.add(Check("source_has_video", summary["has_video"], f"video={'yes' if summary['has_video'] else 'no'}"))
    report.add(
        Check(
            "source_has_audio",
            summary["has_audio"],
            "audio present" if summary["has_audio"] else "no audio stream; transcription cannot run",
        )
    )
    duration = summary.get("container_duration") or 0.0
    report.add(Check("source_duration", duration > 0, f"{duration:.2f}s"))
    return report


def summarise(reports: Sequence[ValidationReport]) -> Dict[str, Any]:
    """Merge several reports into the shape stored in the result manifest."""
    merged = ValidationReport()
    for report in reports:
        merged.extend(report.checks)
    return merged.as_dict()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
