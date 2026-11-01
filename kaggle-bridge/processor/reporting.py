"""Benchmark and run reporting.

Every number in the report is either measured during the run or explicitly
marked ``not measured``. Nothing is estimated to look better: the report exists
to answer "is Kaggle actually faster than the Ryzen 5 4600U for *this* video",
and a guessed number would defeat the purpose.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class Phase:
    """One timed stage of the run."""

    name: str
    seconds: float
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "seconds": round(self.seconds, 3), "details": dict(self.details)}


class PhaseTimer:
    """Context manager that records a phase in a collector."""

    def __init__(self, collector: "BenchmarkCollector", name: str) -> None:
        self.collector = collector
        self.name = name
        self.details: Dict[str, Any] = {}
        self._started = 0.0

    def __enter__(self) -> "PhaseTimer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = time.perf_counter() - self._started
        if exc_type is not None:
            self.details["error"] = f"{exc_type.__name__}: {exc}"
        self.collector.add(Phase(self.name, elapsed, self.details))
        return False


@dataclass
class BenchmarkCollector:
    phases: List[Phase] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    gpu_peak_mb: Optional[float] = None

    def add(self, phase: Phase) -> None:
        self.phases.append(phase)

    def timer(self, name: str) -> PhaseTimer:
        return PhaseTimer(self, name)

    def phase(self, name: str) -> Optional[Phase]:
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None

    def seconds(self, name: str) -> Optional[float]:
        phase = self.phase(name)
        return round(phase.seconds, 3) if phase else None

    @property
    def total_seconds(self) -> float:
        return round(sum(phase.seconds for phase in self.phases), 3)

    def sample_gpu_memory(self) -> None:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                peak = torch.cuda.max_memory_allocated() / (1024 ** 2)
                self.gpu_peak_mb = round(max(self.gpu_peak_mb or 0.0, peak), 1)
        except Exception:
            pass

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phases": [phase.as_dict() for phase in self.phases],
            "total_seconds": self.total_seconds,
            "gpu_peak_mb": self.gpu_peak_mb,
            "notes": list(self.notes),
        }


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "not measured"
    if value < 1:
        return f"{value * 1000:.0f} ms"
    if value < 60:
        return f"{value:.2f} s"
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes)}m {seconds:.0f}s"


def _fmt_bytes(value: Optional[int]) -> str:
    if not value:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def build_benchmark(
    *,
    environment: Mapping[str, Any],
    job: Mapping[str, Any],
    collector: BenchmarkCollector,
    transcription: Optional[Mapping[str, Any]] = None,
    ai: Optional[Mapping[str, Any]] = None,
    renders: Optional[List[Mapping[str, Any]]] = None,
    source: Optional[Mapping[str, Any]] = None,
    baseline: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the machine-readable benchmark block."""
    renders = list(renders or [])
    cpu_renders = [r for r in renders if r.get("codec_used") == "libx264"]
    gpu_renders = [r for r in renders if r.get("codec_used") == "h264_nvenc"]

    cuda = environment.get("cuda") or {}
    devices = cuda.get("devices") or []
    gpu_name = devices[0].get("name") if devices else None
    gpu_memory_mb = devices[0].get("memory_total_mb") if devices else None

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "gpu": gpu_name,
            "gpu_count": cuda.get("count"),
            "gpu_memory_mb": gpu_memory_mb,
            "driver": cuda.get("driver_version"),
            "cuda_available": (cuda.get("torch") or {}).get("cuda_available"),
            "torch": (cuda.get("torch") or {}).get("version"),
            "cpu": (environment.get("cpu") or {}).get("model"),
            "cpu_count": (environment.get("cpu") or {}).get("logical_count"),
            "ram_total_bytes": (environment.get("memory") or {}).get("total_bytes"),
        },
        "source": dict(source or {}),
        "phases": collector.as_dict(),
        "transcription": dict(transcription or {}) or None,
        "ai": dict(ai or {}) or None,
        "rendering": {
            "clips": renders,
            "libx264_mean_seconds": _mean([r.get("wall_seconds") for r in cpu_renders]),
            "h264_nvenc_mean_seconds": _mean([r.get("wall_seconds") for r in gpu_renders]),
            "cpu_vs_gpu_speedup": _speedup(cpu_renders, gpu_renders),
        },
        "peak_gpu_memory_mb": collector.gpu_peak_mb,
        "baseline_local": dict(baseline) if baseline else {"status": "not measured"},
        "job": {
            "job_id": job.get("job_id"),
            "mode": (job.get("processing") or {}).get("mode"),
            "whisper_model": (job.get("transcription") or {}).get("model"),
            "ai_model": (job.get("ai") or {}).get("model"),
            "render_codec": (job.get("rendering") or {}).get("codec"),
        },
    }


def _mean(values: List[Any]) -> Optional[float]:
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(numbers) / len(numbers), 3) if numbers else None


def _speedup(cpu: List[Mapping[str, Any]], gpu: List[Mapping[str, Any]]) -> Optional[float]:
    cpu_mean = _mean([r.get("wall_seconds") for r in cpu])
    gpu_mean = _mean([r.get("wall_seconds") for r in gpu])
    if not cpu_mean or not gpu_mean:
        return None
    return round(cpu_mean / gpu_mean, 2)


def to_markdown(benchmark: Mapping[str, Any], *, validation: Optional[Mapping[str, Any]] = None) -> str:
    """Render the human-readable report. Missing data says so explicitly."""
    host = benchmark.get("host") or {}
    phases = benchmark.get("phases") or {}
    transcription = benchmark.get("transcription") or {}
    ai = benchmark.get("ai") or {}
    rendering = benchmark.get("rendering") or {}
    baseline = benchmark.get("baseline_local") or {}

    lines: List[str] = [
        "# ClipWright Kaggle bridge — run report",
        "",
        f"Generated: {benchmark.get('generated_at')}",
        "",
        "## Hardware",
        "",
        f"- GPU: {host.get('gpu') or 'none detected'} × {host.get('gpu_count') or 0}"
        + (f" ({host.get('gpu_memory_mb'):.0f} MB)" if isinstance(host.get("gpu_memory_mb"), (int, float)) else ""),
        f"- Driver: {host.get('driver') or 'unknown'}; CUDA available in torch: {host.get('cuda_available')}; torch {host.get('torch')}",
        f"- CPU: {host.get('cpu') or 'unknown'} ({host.get('cpu_count')} logical cores)",
        f"- RAM: {_fmt_bytes(host.get('ram_total_bytes'))}",
        f"- Platform: {host.get('platform')}",
        "",
        "## Timings",
        "",
        "| Phase | Duration |",
        "|---|---|",
    ]
    for phase in phases.get("phases") or []:
        lines.append(f"| {phase.get('name')} | {_fmt_seconds(phase.get('seconds'))} |")
    lines.append(f"| **total** | **{_fmt_seconds(phases.get('total_seconds'))}** |")

    if transcription:
        lines.extend(
            [
                "",
                "## Transcription",
                "",
                f"- Model: {transcription.get('model')} on {transcription.get('device')}",
                f"- Audio: {_fmt_seconds(transcription.get('audio_seconds'))}; "
                f"transcribe: {_fmt_seconds(transcription.get('duration_seconds'))} "
                f"(RTF {transcription.get('real_time_factor')}× realtime)",
                f"- Segments: {transcription.get('segment_count')}, words: {transcription.get('word_count')}, "
                f"language: {transcription.get('language')}",
            ]
        )
    else:
        lines.extend(["", "## Transcription", "", "Not run in this execution."])

    if ai:
        attempts = ai.get("attempts") or []
        lines.extend(
            [
                "",
                "## AI selection",
                "",
                f"- Backend: {ai.get('backend')} · model: {ai.get('model')}",
                f"- Inference: {_fmt_seconds(sum(a.get('seconds', 0) for a in attempts))} "
                f"({len(attempts)} attempt(s))",
                f"- Prompt: {ai.get('prompt_chars')} chars over {ai.get('transcript_lines')} transcript lines",
                f"- Selected segments: {len(ai.get('segments') or [])}; "
                f"rejected: {len(ai.get('rejected_segments') or [])}",
            ]
        )
    else:
        lines.extend(["", "## AI selection", "", "Not run in this execution."])

    clips = rendering.get("clips") or []
    lines.extend(["", "## Rendering", ""])
    if clips:
        lines.extend(
            [
                "| Clip | Codec used | Wall time | Encode RTF | Size |",
                "|---|---|---|---|---|",
            ]
        )
        for clip in clips:
            lines.append(
                f"| {Path(str(clip.get('output_path'))).name} | {clip.get('codec_used')} | "
                f"{_fmt_seconds(clip.get('wall_seconds'))} | "
                f"{clip.get('realtime_factor') if clip.get('realtime_factor') is not None else 'n/a'}× | "
                f"{_fmt_bytes(clip.get('size_bytes'))} |"
            )
        lines.extend(
            [
                "",
                f"- libx264 mean: {_fmt_seconds(rendering.get('libx264_mean_seconds'))}",
                f"- h264_nvenc mean: {_fmt_seconds(rendering.get('h264_nvenc_mean_seconds'))}",
                f"- CPU/GPU speedup: "
                f"{rendering.get('cpu_vs_gpu_speedup') if rendering.get('cpu_vs_gpu_speedup') else 'not measurable'}",
            ]
        )
    else:
        lines.append("No clips were rendered in this execution.")

    if validation:
        lines.extend(["", "## Validation", "", f"- Overall: {'PASS' if validation.get('ok') else 'FAIL'}"])
        for failure in validation.get("failed") or []:
            lines.append(f"- FAILED: {failure.get('name')} — {failure.get('detail')}")
        for warning in validation.get("warnings") or []:
            lines.append(f"- warning: {warning.get('name')} — {warning.get('detail')}")

    lines.extend(["", "## Comparison with the local baseline", ""])
    if baseline.get("status") == "not measured" or not baseline:
        lines.append(
            "Not measured. No local (Ryzen 5 4600U) run was supplied for this job, so **no claim "
            "is made about Kaggle being faster**. Re-run the same source locally with the same "
            "settings and add the numbers to `baseline_local` in the job manifest."
        )
    else:
        for key, value in baseline.items():
            lines.append(f"- {key}: {value}")
        lines.append(
            "")
        lines.append(
            "Compare like with like: same source file, same Whisper model, same CRF/preset and "
            "the same clip ranges."
        )

    lines.extend(["", "## Notes", ""])
    for note in phases.get("notes") or []:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def write_reports(directory: Path, benchmark: Mapping[str, Any], markdown: str, validation: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    benchmark_path = directory / "benchmark.json"
    markdown_path = directory / "REPORT.md"
    benchmark_path.write_text(json.dumps(benchmark, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    paths = {"benchmark": str(benchmark_path), "report": str(markdown_path)}
    if validation is not None:
        validation_path = directory / "validation.json"
        validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        paths["validation"] = str(validation_path)
    return paths


def relative_output_paths(directory: Path) -> List[str]:
    return sorted(
        str(path.relative_to(directory))
        for path in Path(directory).rglob("*")
        if path.is_file()
    )


def environment_for_report() -> Dict[str, Any]:
    return {"cwd": os.getcwd(), "python": platform.python_version()}
