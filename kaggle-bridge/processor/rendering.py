"""Gate D — FFmpeg probing and clip rendering.

Encoding settings are copied from production (``backend/src/media/ffmpeg.py``
and ``backend/src/clip_editor.py`` @ ``a73607d``):

* ``build_final_video_encode_args`` → ``libx264``, ``-preset medium``, ``-crf 19``,
  ``yuv420p``, ``profile high``, ``level 4.1``, ``-r 30``,
  ``-x264-params keyint=120:min-keyint=30:scenecut=40``.
* Export presets → 1080x1920 for tiktok/reels/shorts, 192k audio.
* Editor framing → ``scale=w:h:force_original_aspect_ratio=decrease:flags=lanczos``
  followed by a pad to the target size, ``setsar=1``, ``fps=30``.

NVENC is offered as an *additional* encoder for benchmarking only. It is not a
production default, it is verified with a real one-second encode before use
rather than inferred from ``ffmpeg -encoders`` (a build can list the encoder and
still fail at runtime without a working driver), and any failure falls back to
libx264 with the reason recorded.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

PRODUCTION_PRESET = "medium"
PRODUCTION_CRF = 19
PRODUCTION_FPS = 30
PRODUCTION_AUDIO_BITRATE = "192k"
PRODUCTION_PROFILE = "high"
PRODUCTION_LEVEL = "4.1"
PRODUCTION_X264_PARAMS = "keyint=120:min-keyint=30:scenecut=40"


class RenderingError(RuntimeError):
    """Raised when ffmpeg is missing or a render fails."""


@dataclass
class RenderPreset:
    """Render target. Defaults reproduce a ClipWright vertical export."""

    width: int = 1080
    height: int = 1920
    fps: int | str = PRODUCTION_FPS
    crf: int = PRODUCTION_CRF
    preset: str = PRODUCTION_PRESET
    codec: str = "libx264"
    audio_bitrate: str = PRODUCTION_AUDIO_BITRATE
    audio_sample_rate: int = 48000
    mode: str = "pad"  # "pad" (letterbox, production default) or "crop" (fill)

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise RenderingError("width and height must be positive")
        if self.width % 2 or self.height % 2:
            raise RenderingError("width and height must be even for yuv420p")
        if self.codec not in ("libx264", "h264_nvenc"):
            raise RenderingError(f"unsupported codec: {self.codec}")
        if self.mode not in ("pad", "crop"):
            raise RenderingError(f"unsupported framing mode: {self.mode}")


@dataclass
class RenderResult:
    output_path: str
    codec_requested: str
    codec_used: str
    duration_seconds: float
    wall_seconds: float
    size_bytes: int
    command: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "output_path": self.output_path,
            "codec_requested": self.codec_requested,
            "codec_used": self.codec_used,
            "duration_seconds": round(self.duration_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "realtime_factor": round(self.duration_seconds / self.wall_seconds, 3) if self.wall_seconds else None,
            "size_bytes": self.size_bytes,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# capability probing
# --------------------------------------------------------------------------- #


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise RenderingError("ffmpeg is not installed or not on PATH")
    return binary


def _ffprobe() -> str:
    binary = shutil.which("ffprobe")
    if not binary:
        raise RenderingError("ffprobe is not installed or not on PATH")
    return binary


def ffmpeg_version() -> str:
    result = subprocess.run([_ffmpeg(), "-version"], capture_output=True, text=True, timeout=60)
    first = (result.stdout or "").splitlines()
    return first[0].strip() if first else "unknown"


def list_encoders() -> Dict[str, Any]:
    """Return the encoder names ffmpeg advertises, plus the filter list."""
    result = subprocess.run([_ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60)
    encoders = sorted(
        {
            match.group(1)
            for match in re.finditer(r"^\s*[VAS][F.][S.][D.]\s+(\S+)", result.stdout or "", re.MULTILINE)
        }
    )
    filters_result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60
    )
    filters = sorted(
        {
            match.group(1)
            for match in re.finditer(r"^\s*[TSC.]{3}\s+(\S+)", filters_result.stdout or "", re.MULTILINE)
        }
    )
    return {"encoders": encoders, "filters": filters}


def probe_nvenc(working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Actually try an NVENC encode.

    ``ffmpeg -encoders`` reporting ``h264_nvenc`` only means the binary was
    compiled with NVENC support; it does not mean a GPU session can be opened.
    This runs a real one-second encode and returns the outcome.
    """
    info: Dict[str, Any] = {"available": False, "error": None, "probe_seconds": None}
    capabilities = list_encoders()
    if "h264_nvenc" not in capabilities["encoders"]:
        info["error"] = "h264_nvenc is not present in this ffmpeg build"
        return info

    target = (working_dir or Path(".")) / "nvenc_probe.mp4"
    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-t", "1",
        "-c:v", "h264_nvenc", "-preset", "p4", "-pix_fmt", "yuv420p",
        str(target),
    ]
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    info["probe_seconds"] = round(time.perf_counter() - started, 3)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        info["error"] = (result.stderr or "nvenc encode failed").strip().splitlines()[-1:][0][:300]
    else:
        info["available"] = True
    target.unlink(missing_ok=True)
    return info


def ffmpeg_capabilities() -> Dict[str, Any]:
    """Full Gate A/D ffmpeg report."""
    capabilities = list_encoders()
    return {
        "version": ffmpeg_version(),
        "has_libx264": "libx264" in capabilities["encoders"],
        "has_h264_nvenc": "h264_nvenc" in capabilities["encoders"],
        "has_aac": "aac" in capabilities["encoders"],
        "has_ass_filter": "ass" in capabilities["filters"] or "subtitles" in capabilities["filters"],
        "has_scale_filter": "scale" in capabilities["filters"],
        "has_concat_filter": "concat" in capabilities["filters"],
        "encoder_count": len(capabilities["encoders"]),
        "nvenc": probe_nvenc(),
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def build_video_filter(preset: RenderPreset, *, captions_path: Optional[Path] = None) -> str:
    """Assemble the filter chain: framing, SAR, frame rate, optional captions."""
    width, height = preset.width, preset.height
    if preset.mode == "crop":
        framing = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height}"
        )
    else:
        framing = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    chain = [framing, "setsar=1", f"fps={preset.fps}", "format=yuv420p"]
    if captions_path is not None:
        chain.append(f"ass={_escape_filter_path(Path(captions_path))}")
    return ",".join(chain)


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    text = str(path).replace("\\", "/")
    text = text.replace(":", "\\:").replace("'", "\\'")
    return f"'{text}'"


def build_encode_args(preset: RenderPreset, *, codec: Optional[str] = None) -> List[str]:
    """Encoder arguments, mirroring production for libx264 and translating to NVENC."""
    chosen = codec or preset.codec
    if chosen == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-rc", "vbr",
            "-cq", str(preset.crf),
            "-b:v", "0",
            "-profile:v", "high",
            "-level", PRODUCTION_LEVEL,
            "-pix_fmt", "yuv420p",
            "-r", str(preset.fps),
        ]
    return [
        "-c:v", "libx264",
        "-preset", preset.preset,
        "-crf", str(preset.crf),
        "-pix_fmt", "yuv420p",
        "-profile:v", PRODUCTION_PROFILE,
        "-level", PRODUCTION_LEVEL,
        "-r", str(preset.fps),
        "-x264-params", PRODUCTION_X264_PARAMS,
    ]


def build_audio_args(preset: RenderPreset) -> List[str]:
    return [
        "-c:a", "aac",
        "-b:a", preset.audio_bitrate,
        "-ar", str(preset.audio_sample_rate),
        "-ac", "2",
    ]


def media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            _ffprobe(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return 0.0


def render_clip(
    source: Path,
    start_seconds: float,
    end_seconds: float,
    output: Path,
    preset: Optional[RenderPreset] = None,
    *,
    captions_path: Optional[Path] = None,
    allow_nvenc_fallback: bool = True,
) -> RenderResult:
    """Render one clip between two timestamps.

    ``-ss`` precedes ``-i`` so the seek is fast, and because the stream is
    re-encoded the output still starts exactly on the requested frame.
    """
    preset = preset or RenderPreset()
    preset.validate()
    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise RenderingError(f"source video not found: {source}")
    if end_seconds <= start_seconds:
        raise RenderingError(f"invalid range: {start_seconds} → {end_seconds}")
    if captions_path is not None and not Path(captions_path).is_file():
        raise RenderingError(f"captions file not found: {captions_path}")

    output.parent.mkdir(parents=True, exist_ok=True)
    duration = float(end_seconds) - float(start_seconds)
    warnings: List[str] = []

    codec_used = preset.codec
    if codec_used == "h264_nvenc":
        nvenc = probe_nvenc(output.parent)
        if not nvenc["available"]:
            if not allow_nvenc_fallback:
                raise RenderingError(f"NVENC requested but unusable: {nvenc['error']}")
            warnings.append(f"NVENC unavailable ({nvenc['error']}); fell back to libx264")
            codec_used = "libx264"

    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{float(start_seconds):.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", build_video_filter(preset, captions_path=captions_path),
        *build_encode_args(preset, codec=codec_used),
        "-c:a", "aac", "-b:a", preset.audio_bitrate,
        "-ar", str(preset.audio_sample_rate), "-ac", "2",
        "-movflags", "+faststart",
        "-map", "0:v:0", "-map", "0:a:0?",
        str(output),
    ]

    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    wall = time.perf_counter() - started
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        message = (result.stderr or "").strip().splitlines()[-4:]
        raise RenderingError("ffmpeg render failed: " + " / ".join(message)[:600])

    return RenderResult(
        output_path=str(output),
        codec_requested=preset.codec,
        codec_used=codec_used,
        duration_seconds=duration,
        wall_seconds=wall,
        size_bytes=output.stat().st_size,
        command=command,
        warnings=warnings,
    )


def concat_clips(clips: Sequence[Path], output: Path, preset: Optional[RenderPreset] = None) -> RenderResult:
    """Concatenate rendered clips with a re-encode so frame pacing stays uniform."""
    preset = preset or RenderPreset()
    if not clips:
        raise RenderingError("nothing to concatenate")
    for clip in clips:
        if not Path(clip).is_file():
            raise RenderingError(f"missing clip for concatenation: {clip}")

    inputs: List[str] = []
    for clip in clips:
        inputs.extend(["-i", str(clip)])
    streams = "".join(f"[{index}:v:0][{index}:a:0]" for index in range(len(clips)))
    filter_complex = f"{streams}concat=n={len(clips)}:v=1:a=1[v][a]"

    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *build_encode_args(preset, codec=preset.codec),
        "-c:a", "aac", "-b:a", preset.audio_bitrate,
        "-ar", str(preset.audio_sample_rate), "-ac", "2",
        "-movflags", "+faststart",
        str(output),
    ]
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    wall = time.perf_counter() - started
    if result.returncode != 0 or not Path(output).exists():
        raise RenderingError(
            "ffmpeg concat failed: " + (result.stderr or "").strip()[-400:]
        )
    total = sum(media_duration(Path(clip)) for clip in clips)
    return RenderResult(
        output_path=str(output),
        codec_requested=preset.codec,
        codec_used=preset.codec,
        duration_seconds=total,
        wall_seconds=wall,
        size_bytes=Path(output).stat().st_size,
        command=command,
    )


def mux_audio(video: Path, audio: Path, output: Path, preset: Optional[RenderPreset] = None) -> RenderResult:
    """Replace the audio track without re-encoding video."""
    preset = preset or RenderPreset()
    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", preset.audio_bitrate, "-ar", str(preset.audio_sample_rate), "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(output),
    ]
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    wall = time.perf_counter() - started
    if result.returncode != 0 or not Path(output).exists():
        raise RenderingError("ffmpeg audio mux failed: " + (result.stderr or "").strip()[-400:])
    return RenderResult(
        output_path=str(output),
        codec_requested="copy",
        codec_used="copy",
        duration_seconds=media_duration(Path(output)),
        wall_seconds=wall,
        size_bytes=Path(output).stat().st_size,
        command=command,
    )


def extract_audio(video: Path, output: Path, *, bitrate: str = PRODUCTION_AUDIO_BITRATE) -> Path:
    """Extract a stereo 48 kHz AAC track (used to prove audio survives a round trip)."""
    command = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video), "-vn",
        "-c:a", "aac", "-b:a", bitrate, "-ar", "48000", "-ac", "2",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not Path(output).exists():
        raise RenderingError("ffmpeg audio extraction failed: " + (result.stderr or "").strip()[-400:])
    return Path(output)


def write_benchmark_commands(preset: RenderPreset) -> Dict[str, List[str]]:
    """The exact commands used for the CPU/GPU comparison, for the report."""
    return {
        "libx264": build_encode_args(preset, codec="libx264"),
        "h264_nvenc": build_encode_args(preset, codec="h264_nvenc"),
    }


def probe_streams(path: Path) -> Dict[str, Any]:
    """Thin wrapper so callers do not need to import validation for a quick look."""
    result = subprocess.run(
        [
            _ffprobe(), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RenderingError(f"ffprobe failed for {path}: {(result.stderr or '').strip()[:300]}")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    return {
        "format": payload.get("format") or {},
        "video": next((s for s in streams if s.get("codec_type") == "video"), None),
        "audio": next((s for s in streams if s.get("codec_type") == "audio"), None),
    }
