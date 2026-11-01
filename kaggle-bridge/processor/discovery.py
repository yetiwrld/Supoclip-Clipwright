"""Locate the bridge package, the job manifest and the input video.

Kaggle mounts every attached dataset under ``/kaggle/input/<dataset-slug>/`` and
the mount point is *not* the zip's name: it is derived from the dataset slug, and
a dataset can contain one extra directory level depending on how it was uploaded.
Hard-coding a path therefore works on exactly one machine, once.

Everything here is pure path logic so it can be tested against a fake mount tree
instead of a real GPU notebook. The kernel wraps these helpers and turns
``DiscoveryError`` into its own exit message.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

#: Where Kaggle mounts attached datasets inside a notebook.
DEFAULT_INPUT_ROOT = Path("/kaggle/input")
#: Where the kernel process starts; hand-placed files land here.
DEFAULT_WORKING_ROOT = Path("/kaggle/working")
#: The file that proves a directory is the bridge root.
PACKAGE_MARKER = Path("processor") / "__init__.py"
#: Container formats the bridge will transcribe.
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")

#: Deep enough for ``/kaggle/input/<slug>/<archive-root>/kaggle-bridge`` and no
#: deeper: an unbounded walk over a mounted dataset is slow and can follow
#: symlinks out of the mount.
MAX_SEARCH_DEPTH = 4

_SKIP_DIRECTORIES = {"__pycache__", ".git", ".ipynb_checkpoints", "node_modules"}


class DiscoveryError(Exception):
    """Raised when something the job needs cannot be located unambiguously."""


def _walk(root: Path, max_depth: int) -> Iterable[Path]:
    """Yield directories under ``root`` (inclusive) without following symlinks."""
    root = Path(root)
    if not root.is_dir():
        return
    base_depth = len(root.parts)
    for current, directories, _files in os.walk(root, followlinks=False):
        path = Path(current)
        depth = len(path.parts) - base_depth
        directories[:] = sorted(d for d in directories if d not in _SKIP_DIRECTORIES)
        yield path
        if depth >= max_depth:
            directories[:] = []


def _walk_files(root: Path, max_depth: int) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return
    base_depth = len(root.parts)
    for current, directories, files in os.walk(root, followlinks=False):
        path = Path(current)
        depth = len(path.parts) - base_depth
        directories[:] = sorted(d for d in directories if d not in _SKIP_DIRECTORIES)
        for name in sorted(files):
            yield path / name
        if depth >= max_depth:
            directories[:] = []


# --------------------------------------------------------------------------- #
# the code dataset
# --------------------------------------------------------------------------- #


def is_bridge_root(candidate: Path) -> bool:
    return (Path(candidate) / PACKAGE_MARKER).is_file()


def find_bridge_root(
    *,
    override: Optional[os.PathLike | str] = None,
    search_roots: Sequence[os.PathLike | str] = (),
    max_depth: int = MAX_SEARCH_DEPTH,
) -> Path:
    """Return the directory that contains ``processor/__init__.py``.

    Search order: the explicit override, then each search root and its
    descendants (breadth-first by depth, so a shallow match always wins over a
    stray copy nested deeper inside a dataset).
    """
    tried: List[str] = []

    if override:
        candidate = Path(override)
        tried.append(str(candidate))
        if is_bridge_root(candidate):
            return candidate.resolve()
        if is_bridge_root(candidate / "kaggle-bridge"):
            return (candidate / "kaggle-bridge").resolve()
        raise DiscoveryError(
            f"BRIDGE_PATH is set to {candidate} but it contains no 'processor/__init__.py'. "
            "Point it at the bridge root (the folder holding 'processor/') or unset it."
        )

    roots = [Path(root) for root in search_roots]
    for depth in range(max_depth + 1):
        for root in roots:
            if not root.exists():
                tried.append(f"{root} (absent)")
                continue
            for directory in _walk_bounded(root, depth):
                if is_bridge_root(directory):
                    return directory.resolve()
            tried.append(f"{root} (depth {depth})")

    raise DiscoveryError(
        "Could not find the 'processor' package. Expected the private code dataset to "
        "contain 'kaggle-bridge/processor/__init__.py' under "
        f"{DEFAULT_INPUT_ROOT}/<dataset>/, or set BRIDGE_PATH. Searched: "
        + ", ".join(tried[:8])
        + (" ..." if len(tried) > 8 else "")
    )


def _walk_bounded(root: Path, depth: int) -> Iterable[Path]:
    """Yield directories of exactly ``depth`` below ``root``."""
    yield from (path for path in _walk(root, depth) if len(path.parts) - len(root.parts) == depth)


# --------------------------------------------------------------------------- #
# the job manifest
# --------------------------------------------------------------------------- #


def find_job_manifest(
    *,
    explicit: Optional[os.PathLike | str] = None,
    working_dirs: Sequence[os.PathLike | str] = (),
    input_root: Optional[os.PathLike | str] = DEFAULT_INPUT_ROOT,
    max_depth: int = 3,
    is_valid_job=None,
) -> Path:
    """Return the job manifest path.

    ``is_valid_job`` is an optional predicate ``(payload) -> bool`` used to reject
    JSON files that merely look like manifests; the kernel passes a schema check.
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise DiscoveryError(f"job manifest not found: {path}")
        return path

    for directory in working_dirs:
        candidate = Path(directory) / "job.json"
        if candidate.is_file():
            return candidate

    if input_root is None:
        raise DiscoveryError("no job manifest given and no input root to search")

    root = Path(input_root)
    if not root.is_dir():
        raise DiscoveryError(
            f"no job manifest given and {root} does not exist. Attach a private dataset "
            "that contains job.json, or copy config/sample-job.json to "
            f"{DEFAULT_WORKING_ROOT}/job.json."
        )

    # Three preference tiers, because the code dataset ships config/sample-job.json:
    # a real job.json must always beat the bundled example, whatever the mount order.
    real: List[Path] = []
    examples: List[Path] = []
    others: List[Path] = []
    for path in _walk_files(root, max_depth):
        if path.suffix.lower() != ".json":
            continue
        if path.name == "job.json":
            real.append(path)
        elif path.name == "sample-job.json":
            examples.append(path)
        elif "job" in path.name.lower():
            others.append(path)

    for path in real + examples + others:
        if is_valid_job is None:
            return path
        try:
            if is_valid_job(_load_json(path)):
                return path
        except (OSError, ValueError):
            continue

    raise DiscoveryError(
        f"No job manifest found under {root}. Attach a private dataset containing "
        "job.json, or place one at "
        f"{DEFAULT_WORKING_ROOT}/job.json."
    )


def _load_json(path: Path):
    import json

    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# the input video
# --------------------------------------------------------------------------- #


def describe_media(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return str(path)
    return f"{path} ({size / (1024 * 1024):.1f} MB)"


def sha256_of(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def find_input_media(
    *,
    filename: str,
    explicit_path: Optional[os.PathLike | str] = None,
    input_root: Optional[os.PathLike | str] = DEFAULT_INPUT_ROOT,
    declared_size: Optional[int] = None,
    max_depth: int = 3,
) -> Path:
    """Return the source video, refusing to guess between several candidates.

    Order: the job's explicit ``source.path``, then an exact filename match
    anywhere under the input root, then a single video file — but if several
    videos are mounted and none matches by name, the caller gets a list instead of
    a silently wrong clip.
    """
    if not filename and not explicit_path:
        raise DiscoveryError("job.source.filename is required for local_file sources")

    if explicit_path:
        direct = Path(explicit_path)
        if direct.is_file():
            return direct

    root = Path(input_root) if input_root is not None else None
    if root is not None and root.is_dir():
        if filename:
            exact = sorted(
                path
                for path in _walk_files(root, max_depth)
                if path.name == filename and path.is_file()
            )
            if exact:
                matched = _match_by_size(exact, declared_size)
                return matched or exact[0]

        videos = sorted(
            path
            for path in _walk_files(root, max_depth)
            if path.suffix.lower() in VIDEO_SUFFIXES
        )
        if len(videos) == 1:
            return videos[0]
        if len(videos) > 1:
            matched = _match_by_size(videos, declared_size)
            if matched is not None:
                return matched
            raise DiscoveryError(
                f"several videos are mounted under {root} and none matches "
                f"{filename!r}: " + "; ".join(describe_media(path) for path in videos[:6])
                + ". Set source.filename (or source.sha256/size_bytes) in job.json, or "
                "attach only the dataset you mean."
            )

    where = f"under {root}" if root is not None else "(no input root configured)"
    raise DiscoveryError(
        f"input video {filename!r} was not found {where}. Attach the private dataset "
        "that contains it, and check the filename in job.json."
    )


def _match_by_size(candidates: Sequence[Path], declared_size: Optional[int]) -> Optional[Path]:
    """Return the candidate whose byte size equals ``declared_size``, if any."""
    if not declared_size:
        return None
    for path in candidates:
        try:
            if path.stat().st_size == int(declared_size):
                return path
        except OSError:
            continue
    return None
