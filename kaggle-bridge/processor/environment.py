"""Gate A — hardware and environment report.

The report exists for one reason: a GPU run that silently fell back to CPU would
produce numbers that look plausible and are worthless. So the kernel refuses to
continue when it was asked for GPU execution and CUDA is not available, and every
report carries the raw evidence (device name, driver version, torch build) next
to the timings.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import rendering, transcription


class GpuUnavailableError(RuntimeError):
    """Raised when GPU execution was requested but no usable CUDA device exists."""


@dataclass
class EnvironmentReport:
    payload: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    @property
    def cuda_available(self) -> bool:
        return bool(self.payload.get("cuda", {}).get("available"))

    def as_dict(self) -> Dict[str, Any]:
        return {**self.payload, "problems": list(self.problems)}


def _run(command: List[str], timeout: int = 60) -> Optional[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def memory_total_bytes() -> Optional[int]:
    """Total RAM without psutil: /proc/meminfo on Linux, sysconf elsewhere."""
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                try:
                    return int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return None


def memory_available_bytes() -> Optional[int]:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                try:
                    return int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    return None
    return None


def disk_report() -> Dict[str, Any]:
    """Free/total disk for the paths that matter on Kaggle."""
    report: Dict[str, Any] = {}
    for name, raw in (
        ("working", "/kaggle/working"),
        ("input", "/kaggle/input"),
        ("tmp", "/tmp"),
        ("cwd", os.getcwd()),
    ):
        target = Path(raw)
        if not target.exists():
            report[name] = None
            continue
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            report[name] = None
            continue
        report[name] = {
            "path": str(target),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }
    return report


def gpu_report() -> Dict[str, Any]:
    """GPU facts from nvidia-smi, then from torch."""
    report: Dict[str, Any] = {
        "nvidia_smi": None,
        "devices": [],
        "driver_version": None,
        "cuda_version_smi": None,
        "count": 0,
    }

    smi = shutil.which("nvidia-smi")
    if smi:
        query = _run(
            [
                smi,
                "--query-gpu=index,name,memory.total,memory.used,driver_version,compute_cap,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        if query:
            report["nvidia_smi"] = query
            for line in query.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) < 5:
                    continue
                device = {
                    "index": parts[0],
                    "name": parts[1],
                    "memory_total_mb": _safe_float(parts[2]),
                    "memory_used_mb": _safe_float(parts[3]),
                    "driver_version": parts[4],
                    "compute_capability": parts[5] if len(parts) > 5 else None,
                    "utilization_percent": _safe_float(parts[6]) if len(parts) > 6 else None,
                }
                report["devices"].append(device)
            report["count"] = len(report["devices"])
            if report["devices"]:
                report["driver_version"] = report["devices"][0]["driver_version"]

        header = _run([smi])
        if header:
            for line in header.splitlines():
                if "CUDA Version" in line:
                    report["cuda_version_smi"] = line.split("CUDA Version:")[-1].strip().split()[0]

    torch_info = torch_report()
    report["torch"] = torch_info
    if not report["devices"] and torch_info.get("device_names"):
        report["devices"] = [
            {"index": str(index), "name": name, "memory_total_mb": None, "memory_used_mb": None,
             "driver_version": None, "compute_capability": None, "utilization_percent": None}
            for index, name in enumerate(torch_info["device_names"])
        ]
        report["count"] = len(report["devices"])
    return report


def torch_report() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "installed": False,
        "version": None,
        "cuda_build": None,
        "cuda_available": False,
        "device_count": 0,
        "device_names": [],
        "cudnn_version": None,
        "capabilities": [],
    }
    try:
        import torch  # type: ignore
    except Exception as error:  # pragma: no cover - only when torch is missing
        info["error"] = f"{type(error).__name__}: {error}"
        return info

    info["installed"] = True
    info["version"] = getattr(torch, "__version__", None)
    info["cuda_build"] = getattr(getattr(torch, "version", None), "cuda", None)
    try:
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            info["device_count"] = int(torch.cuda.device_count())
            info["device_names"] = [torch.cuda.get_device_name(i) for i in range(info["device_count"])]
            info["capabilities"] = [
                ".".join(str(part) for part in torch.cuda.get_device_capability(i))
                for i in range(info["device_count"])
            ]
            try:
                info["cudnn_version"] = torch.backends.cudnn.version()
            except Exception:  # pragma: no cover
                pass
    except Exception as error:  # pragma: no cover
        info["error"] = f"{type(error).__name__}: {error}"
    return info


def ytdlp_report() -> Dict[str, Any]:
    try:
        from yt_dlp.version import __version__ as ytdlp_version  # type: ignore
    except Exception as error:  # pragma: no cover
        return {"installed": False, "version": None, "error": f"{type(error).__name__}: {error}"}
    return {"installed": True, "version": ytdlp_version}


def collect(*, probe_ffmpeg: bool = True) -> EnvironmentReport:
    """Collect the full Gate A report."""
    report = EnvironmentReport()

    report.payload.update(
        {
            "python": {
                "version": sys.version.split()[0],
                "executable": sys.executable,
                "implementation": platform.python_implementation(),
            },
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "cpu": {
                "logical_count": os.cpu_count(),
                "model": _cpu_model(),
            },
            "memory": {
                "total_bytes": memory_total_bytes(),
                "available_bytes": memory_available_bytes(),
            },
            "disk": disk_report(),
            "kaggle": {
                key: os.environ.get(key)
                for key in (
                    "KAGGLE_KERNEL_RUN_TYPE",
                    "KAGGLE_URL_BASE",
                    "CUDA_VISIBLE_DEVICES",
                    "KAGGLE_DATA_PROXY_URL",
                )
            },
            "whisper": transcription.whisper_environment(),
            "ytdlp": ytdlp_report(),
        }
    )

    if probe_ffmpeg:
        try:
            report.payload["ffmpeg"] = rendering.ffmpeg_capabilities()
        except Exception as error:
            report.payload["ffmpeg"] = {"error": f"{type(error).__name__}: {error}"}
            report.problems.append(f"ffmpeg probe failed: {error}")
    else:
        report.payload["ffmpeg"] = {"skipped": True}

    report.payload["cuda"] = gpu_report()

    if not report.payload["cuda"]["torch"]["installed"]:
        report.problems.append("torch is not installed")
    elif not report.cuda_available:
        report.problems.append("torch reports CUDA as unavailable")
    elif report.payload["cuda"]["count"] < 1:
        report.problems.append("no CUDA devices were enumerated")

    ffmpeg_info = report.payload.get("ffmpeg") or {}
    if ffmpeg_info.get("error"):
        report.problems.append("ffmpeg is not usable")
    elif probe_ffmpeg and not ffmpeg_info.get("has_libx264"):
        report.problems.append("this ffmpeg build has no libx264 encoder")

    whisper_info = report.payload.get("whisper") or {}
    if not whisper_info.get("importable"):
        report.problems.append("the 'whisper' module is not importable")
    elif whisper_info.get("collision"):
        report.problems.append("multiple distributions provide the 'whisper' module")
    elif not whisper_info.get("has_load_model"):
        report.problems.append("the imported 'whisper' module is not openai-whisper")

    return report


def require_gpu(report: EnvironmentReport, *, mode: str = "gpu") -> None:
    """Fail fast instead of silently benchmarking on CPU."""
    if mode != "gpu":
        return
    if not report.cuda_available:
        torch_info = report.payload.get("cuda", {}).get("torch", {})
        raise GpuUnavailableError(
            "GPU execution was requested but CUDA is unavailable in this kernel. "
            f"torch={torch_info.get('version')!r} built for CUDA {torch_info.get('cuda_build')!r}, "
            f"device_count={torch_info.get('device_count')}. "
            "Enable the GPU accelerator in kernel-metadata.json (machine_shape: NvidiaTeslaT4) "
            "and re-push; do not report CPU timings as GPU results."
        )


def to_markdown(report: EnvironmentReport) -> str:
    """Human-readable Gate A section for the run report."""
    payload = report.payload
    cuda = payload.get("cuda", {})
    torch_info = cuda.get("torch", {})
    ffmpeg_info = payload.get("ffmpeg", {})
    whisper_info = payload.get("whisper", {})
    memory = payload.get("memory", {})
    disk = payload.get("disk", {})

    lines = [
        "## Gate A — environment",
        "",
        f"- Python: {payload.get('python', {}).get('version')} ({payload.get('python', {}).get('implementation')})",
        f"- OS: {payload.get('os', {}).get('system')} {payload.get('os', {}).get('release')} ({payload.get('os', {}).get('machine')})",
        f"- CPU: {payload.get('cpu', {}).get('logical_count')} logical cores — {payload.get('cpu', {}).get('model') or 'unknown model'}",
        f"- RAM: {_gb(memory.get('total_bytes'))} total, {_gb(memory.get('available_bytes'))} available",
        f"- Disk (working): {_gb((disk.get('working') or {}).get('free_bytes'))} free of {_gb((disk.get('working') or {}).get('total_bytes'))}",
        f"- GPU: {cuda.get('count')} device(s)"
        + (f" — {', '.join(d['name'] for d in cuda.get('devices', []))}" if cuda.get("devices") else ""),
        f"- NVIDIA driver: {cuda.get('driver_version') or 'unknown'}; CUDA (driver): {cuda.get('cuda_version_smi') or 'unknown'}",
        f"- PyTorch: {torch_info.get('version')} (CUDA build {torch_info.get('cuda_build')}), "
        f"CUDA available: {torch_info.get('cuda_available')}, cuDNN: {torch_info.get('cudnn_version')}",
        f"- Whisper module: {whisper_info.get('module_path')} "
        f"(providers: {', '.join(whisper_info.get('distributions') or []) or 'unknown'})",
        f"- FFmpeg: {ffmpeg_info.get('version')}",
        f"- Encoders: libx264={ffmpeg_info.get('has_libx264')}, "
        f"h264_nvenc listed={ffmpeg_info.get('has_h264_nvenc')}, "
        f"nvenc usable={(ffmpeg_info.get('nvenc') or {}).get('available')}",
        f"- yt-dlp: {(payload.get('ytdlp') or {}).get('version')}",
    ]
    if report.problems:
        lines.extend(["", "**Environment problems**", ""] + [f"- {problem}" for problem in report.problems])
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: EnvironmentReport) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return path


def _cpu_model() -> Optional[str]:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[-1].strip()
    return platform.processor() or None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _gb(value: Optional[int]) -> str:
    if not value:
        return "unknown"
    return f"{value / (1024 ** 3):.1f} GB"
