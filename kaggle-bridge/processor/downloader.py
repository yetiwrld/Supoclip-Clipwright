"""Gate E — yt-dlp downloading, isolated from the production downloaders.

Rules encoded here:

* Only an operator-supplied URL is fetched. No search, no channel crawling, no
  playlist expansion.
* No authentication bypass. Private, age-gated, members-only or DRM-protected
  sources fail with a message that says so, and the bridge does not retry with
  credentials, cookies or a proxy.
* Hard limits on size, duration and wall-clock time, enforced *before* the file
  is kept.
* No paid service (no Apify, no RapidAPI, no residential proxies). The production
  ``rapidapi_youtube_downloader`` is deliberately not mirrored.

The production YouTube path additionally solves player JS challenges with deno
and a PO-token provider; Kaggle's stock image does not ship that toolchain, so
downloads that need it will fail with a clear message rather than silently
producing a 403. Local-file jobs are unaffected.
"""

from __future__ import annotations

import os
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB default ceiling
MAX_DURATION_SECONDS = 3 * 3600
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b"

#: Errors that mean "this source will not work without bypassing a restriction",
#: which the bridge refuses to do.
_RESTRICTED_MARKERS = (
    "private video",
    "sign in to confirm",
    "members-only",
    "age-restricted",
    "age restricted",
    "this video is unavailable",
    "requires payment",
    "drm",
    "login required",
)


class DownloadError(RuntimeError):
    """Raised when a download fails, exceeds a limit, or is restricted."""


@dataclass
class DownloadResult:
    path: str
    url: str
    title: Optional[str]
    duration_seconds: Optional[float]
    size_bytes: int
    wall_seconds: float
    extractor: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "title": self.title,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "wall_seconds": round(self.wall_seconds, 3),
            "extractor": self.extractor,
            "warnings": list(self.warnings),
        }


def internet_available(timeout: float = 5.0) -> bool:
    """Cheap reachability probe; Kaggle kernels can have internet disabled."""
    for host, port in (("1.1.1.1", 53), ("pypi.org", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _require_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as error:  # pragma: no cover
        raise DownloadError(
            "yt-dlp is not installed in this environment; run: pip install -r requirements.txt"
        ) from error
    return yt_dlp


def fetch_metadata(url: str, *, timeout_seconds: int = 120) -> Dict[str, Any]:
    """Read metadata without downloading the media."""
    yt_dlp = _require_ytdlp()
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout_seconds,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as error:  # yt_dlp raises its own hierarchy
        raise DownloadError(_humanise(error, url)) from error

    if info is None:
        raise DownloadError(f"no metadata returned for {url}")
    if info.get("_type") == "playlist":
        raise DownloadError("playlists are not supported; pass a single video URL")
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration_seconds": info.get("duration"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "uploader": info.get("uploader"),
        "is_live": info.get("is_live"),
        "availability": info.get("availability"),
        "webpage_url": info.get("webpage_url"),
    }


def download(
    url: str,
    output_dir: Path,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    max_duration_seconds: int = MAX_DURATION_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    format_selector: str = DEFAULT_FORMAT,
    retries: int = 2,
    audio_only: bool = False,
) -> DownloadResult:
    """Download one video with hard size, duration and time limits."""
    yt_dlp = _require_ytdlp()
    if not url.startswith(("https://", "http://")):
        raise DownloadError("only http(s) URLs are accepted")
    if not internet_available():
        raise DownloadError(
            "the kernel has no internet access; enable it for this run or supply a local file"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = output_dir / f".download-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=True)

    metadata = fetch_metadata(url, timeout_seconds=min(timeout_seconds, 180))
    if metadata.get("is_live"):
        raise DownloadError("live streams are not supported")
    duration = metadata.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > max_duration_seconds:
        raise DownloadError(
            f"source is {int(duration)}s, above the {max_duration_seconds}s limit"
        )

    downloaded: Dict[str, Any] = {}

    def _on_progress(status: Dict[str, Any]) -> None:
        if status.get("status") == "finished":
            downloaded["filename"] = status.get("filename")
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if total and total > max_bytes:
            # Raising from the hook aborts the download cleanly.
            raise DownloadError(
                f"download would exceed the {max_bytes} byte limit (reported {int(total)} bytes)"
            )

    options: Dict[str, Any] = {
        "outtmpl": str(staging / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": 60,
        "retries": retries,
        "fragment_retries": retries,
        "progress_hooks": [_on_progress],
        "format": "bestaudio/best" if audio_only else format_selector,
        "max_filesize": max_bytes,
        "overwrites": True,
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
            if audio_only
            else [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        ),
    }

    started = time.perf_counter()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise DownloadError(_humanise(error, url)) from error
    wall = time.perf_counter() - started

    candidates = [path for path in staging.iterdir() if path.is_file()]
    if not candidates:
        shutil.rmtree(staging, ignore_errors=True)
        raise DownloadError("yt-dlp reported success but produced no file")

    produced = max(candidates, key=lambda path: path.stat().st_size)
    size = produced.stat().st_size
    if size > max_bytes:
        produced.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise DownloadError(f"downloaded file is {size} bytes, above the {max_bytes} byte limit")

    final_path = output_dir / produced.name
    shutil.move(str(produced), str(final_path))
    shutil.rmtree(staging, ignore_errors=True)

    warnings: List[str] = []
    if info and info.get("_type") == "playlist":
        warnings.append("playlist metadata was returned; only the first entry was downloaded")

    return DownloadResult(
        path=str(final_path),
        url=url,
        title=(info or {}).get("title") or metadata.get("title"),
        duration_seconds=(info or {}).get("duration") or duration,
        size_bytes=final_path.stat().st_size,
        wall_seconds=wall,
        extractor=(info or {}).get("extractor_key") or metadata.get("extractor"),
        metadata=metadata,
        warnings=warnings,
    )


def _humanise(error: Exception, url: str) -> str:
    """Turn a yt-dlp traceback into an actionable sentence."""
    text = str(error)
    lowered = text.lower()
    for marker in _RESTRICTED_MARKERS:
        if marker in lowered:
            return (
                f"source is restricted ({marker}). The bridge does not bypass access "
                f"controls; download it yourself and pass it as a local file. URL: {url}"
            )
    if "javascript" in lowered or "nsig" in lowered or "po_token" in lowered:
        return (
            "YouTube rejected the request because the player challenge could not be solved. "
            "Kaggle's image has no deno/PO-token provider; download locally and supply a file. "
            f"Original error: {text[:300]}"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"download timed out: {text[:300]}"
    return f"download failed: {text[:300]}"
