"""Packaging and kernel-metadata safeguards for the Kaggle bridge.

The PowerShell scripts in ``scripts/`` are thin wrappers around this module. That
is deliberate: PowerShell cannot be executed in CI (or on the machines this was
developed on), so anything that can silently corrupt an upload lives here, in
plain Python, where it is unit-tested.

Two jobs:

* **Zip packages.** ``Compress-Archive`` cannot store a file larger than 2 GB, and
  Kaggle limits are not the same thing. Here archives are written with
  :mod:`zipfile` and ZIP64 enabled, so the only limit is an explicit, documented
  one that fails *before* packaging starts.
* **Kernel metadata checks.** A wrong ``is_private``, a CPU ``machine_shape`` or a
  leftover placeholder is the difference between a private run and a public
  upload. :func:`validate_kernel_metadata` returns every problem at once so the
  operator fixes them in one pass.

Run as::

    python -m processor.packaging limits
    python -m processor.packaging check-metadata --metadata kernel/kernel-metadata.json \\
        --kernel-dir kernel --strict
    python -m processor.packaging zip-tree --source dist/stage/kaggle-bridge \\
        --destination dist/clipwright-bridge-code.zip --arc-root kaggle-bridge
    python -m processor.packaging zip-input --video D:\\clips\\a.mp4 --job dist/job.json \\
        --destination dist/clipwright-phase1-input.zip
    python -m processor.packaging verify-zip --path dist/clipwright-bridge-code.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #

#: Hard ceiling for a single packaged input. A short test clip is 10-200 MB; the
#: bridge's own ``downloader`` refuses sources above 2 GiB, and Kaggle autosaves
#: ~20 GB of output, so 4 GiB is generous headroom that still fails *early* on a
#: mistakenly selected multi-hour recording. Override with ``--max-gb``.
DEFAULT_MAX_INPUT_BYTES = 4 * 1024**3
#: Above this, packaging warns: uploads get slow and jobs get expensive.
RECOMMENDED_MAX_INPUT_BYTES = 1 * 1024**3
#: Kaggle keeps the last 20 GB of ``/kaggle/working``; the kernel stops at 19 GB.
KAGGLE_AUTOSAVE_BYTES = 20 * 1024**3
#: ``Compress-Archive`` fails above this. We do not use it — kept for the docs.
COMPRESS_ARCHIVE_LIMIT_BYTES = 2 * 1024**3

#: Directories never shipped to Kaggle.
EXCLUDED_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".venv",
    "node_modules",
    ".ipynb_checkpoints",
}

PLACEHOLDER_PATTERNS = (
    "your-kaggle-username",
    "your_username",
    "your-username",
    "yourusername",
    "changeme",
    "change-me",
    "example-user",
    "<your",
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")
_KERNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}/[A-Za-z0-9][A-Za-z0-9-]{0,49}$")


class PackagingError(Exception):
    """Raised when a package cannot be built safely."""


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TB"  # pragma: no cover


def check_input_size(size_bytes: int, *, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> Optional[str]:
    """Return a warning for ``size_bytes`` or raise when it exceeds the ceiling.

    The failure happens *before* a multi-gigabyte archive is written, and it names
    the limit and how to change it, because "zip failed" is a useless message at
    that size.
    """
    if size_bytes > max_bytes:
        raise PackagingError(
            f"input is {human_bytes(size_bytes)}, above the {human_bytes(max_bytes)} limit "
            "for a single packaged job. Use a shorter test clip, or raise the limit "
            "explicitly with -MaxInputGb (and remember the kernel caps downloads at 2 GiB "
            "and Kaggle autosaves ~20 GB of output)."
        )
    if size_bytes > RECOMMENDED_MAX_INPUT_BYTES:
        return (
            f"input is {human_bytes(size_bytes)}; large uploads are slow and the first "
            "runs should use a short clip (under "
            f"{human_bytes(RECOMMENDED_MAX_INPUT_BYTES)})"
        )
    return None


def file_sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# zip building
# --------------------------------------------------------------------------- #


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts)


def _iter_tree(source: Path, arc_root: Optional[str]) -> Iterable[tuple[Path, str]]:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            raise PackagingError(
                f"refusing to package symlink {path}: Kaggle would store a dangling target"
            )
        if path.is_file():
            arcname = str(Path(arc_root) / relative) if arc_root else relative.as_posix()
            yield path, arcname.replace(os.sep, "/")


def build_tree_zip(
    source: Path,
    destination: Path,
    *,
    arc_root: Optional[str] = None,
    allow_zip64: bool = True,
) -> Dict[str, Any]:
    """Zip ``source`` (a directory) into ``destination`` with ZIP64 available."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise PackagingError(f"nothing to package: {source} is not a directory")
    entries = list(_iter_tree(source, arc_root))
    if not entries:
        raise PackagingError(f"{source} contains no files to package")
    return _write_zip(destination, entries, allow_zip64=allow_zip64)


def build_file_zip(
    files: Mapping[str, Path] | Sequence[tuple[Path, str]],
    destination: Path,
    *,
    allow_zip64: bool = True,
) -> Dict[str, Any]:
    """Zip explicit files. ``files`` maps arcname -> path, or is (path, arcname) pairs."""
    if isinstance(files, Mapping):
        pairs = [(Path(path), str(arcname)) for arcname, path in files.items()]
    else:
        pairs = [(Path(path), str(arcname)) for path, arcname in files]

    entries: List[tuple[Path, str]] = []
    for path, arcname in pairs:
        if not path.is_file():
            raise PackagingError(f"cannot package missing file: {path}")
        if arcname.startswith("/") or ".." in Path(arcname).parts:
            raise PackagingError(f"unsafe archive name: {arcname}")
        entries.append((path, arcname.replace(os.sep, "/")))
    if not entries:
        raise PackagingError("no files to package")
    return _write_zip(Path(destination), entries, allow_zip64=allow_zip64)


def _write_zip(
    destination: Path,
    entries: Sequence[tuple[Path, str]],
    *,
    allow_zip64: bool,
) -> Dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    total_uncompressed = 0
    largest = 0
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=allow_zip64,
    ) as archive:
        for path, arcname in entries:
            size = path.stat().st_size
            total_uncompressed += size
            largest = max(largest, size)
            if not allow_zip64 and largest > COMPRESS_ARCHIVE_LIMIT_BYTES:
                raise PackagingError(
                    f"{path} is {human_bytes(size)}; ZIP64 is disabled, so the archive "
                    "would be unreadable above 4 GB"
                )
            archive.write(path, arcname)

    summary = verify_zip(destination)
    summary["uncompressed_bytes"] = total_uncompressed
    summary["largest_entry_bytes"] = largest
    return summary


@dataclass(frozen=True)
class ZipSummary:
    path: Path
    entries: int
    compressed_bytes: int
    uncompressed_bytes: int
    largest_entry_bytes: int
    sha256: str
    uses_zip64: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "entries": self.entries,
            "compressed_bytes": self.compressed_bytes,
            "compressed": human_bytes(self.compressed_bytes),
            "uncompressed_bytes": self.uncompressed_bytes,
            "largest_entry_bytes": self.largest_entry_bytes,
            "largest_entry": human_bytes(self.largest_entry_bytes),
            "sha256": self.sha256,
            "zip64": self.uses_zip64,
        }


def verify_zip(path: Path) -> Dict[str, Any]:
    """Re-open the archive and prove it is readable, complete and path-safe."""
    path = Path(path)
    if not path.is_file():
        raise PackagingError(f"archive not found: {path}")
    if path.stat().st_size == 0:
        raise PackagingError(f"archive is empty: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            corrupted = archive.testzip()
            uses_zip64 = any(info.compress_type is not None and info.header_offset > 0xFFFFFFFF for info in archive.infolist()) or any(
                info.file_size > 0xFFFFFFFF for info in archive.infolist()
            )
            infos = archive.infolist()
    except zipfile.BadZipFile as error:
        raise PackagingError(f"{path} is not a readable zip archive: {error}") from error

    if corrupted:
        raise PackagingError(f"{path} is corrupt at entry {corrupted}")
    if not names:
        raise PackagingError(f"{path} contains no entries")
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise PackagingError(f"{path} contains an unsafe entry: {name}")

    return {
        "path": path,
        "entries": len(names),
        "compressed_bytes": path.stat().st_size,
        "uncompressed_bytes": sum(info.file_size for info in infos),
        "largest_entry_bytes": max(info.file_size for info in infos),
        "sha256": file_sha256(path),
        "uses_zip64": uses_zip64,
        "names": names,
    }


def zip_summary(path: Path) -> ZipSummary:
    data = verify_zip(path)
    return ZipSummary(
        path=data["path"],
        entries=data["entries"],
        compressed_bytes=data["compressed_bytes"],
        uncompressed_bytes=data["uncompressed_bytes"],
        largest_entry_bytes=data["largest_entry_bytes"],
        sha256=data["sha256"],
        uses_zip64=data["uses_zip64"],
    )


# --------------------------------------------------------------------------- #
# kernel metadata
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MetadataIssue:
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"

    def __str__(self) -> str:
        tag = "error" if self.severity == "error" else "warn "
        return f"{tag} {self.field}: {self.message}"


@dataclass
class MetadataReport:
    issues: List[MetadataIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[MetadataIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> List[MetadataIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, field_name: str, message: str, severity: str = "error") -> None:
        self.issues.append(MetadataIssue(field_name, message, severity))


def _is_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1")


def _looks_like_placeholder(value: str) -> bool:
    lowered = str(value).strip().lower()
    return any(pattern in lowered for pattern in PLACEHOLDER_PATTERNS)


def validate_kernel_metadata(
    metadata: Mapping[str, Any],
    *,
    kernel_dir: Path,
    code_file: str = "clipwright_worker.py",
    expected_datasets: int = 2,
    require_internet: bool = True,
) -> MetadataReport:
    """Check every field the push depends on, and report all problems at once."""
    report = MetadataReport()
    if not isinstance(metadata, Mapping):
        report.add("metadata", "not a JSON object")
        return report

    # -- identity ----------------------------------------------------------
    kernel_id = str(metadata.get("id") or "")
    if not kernel_id:
        report.add("id", "missing; expected '<owner>/<kernel-slug>'")
    elif _looks_like_placeholder(kernel_id):
        report.add("id", f"still contains a placeholder: {kernel_id!r}")
    elif not _KERNEL_ID_RE.match(kernel_id):
        report.add("id", f"{kernel_id!r} is not '<owner>/<kernel-slug>'")

    # -- code --------------------------------------------------------------
    declared_code = str(metadata.get("code_file") or "")
    if not declared_code:
        report.add("code_file", "missing")
    elif declared_code != code_file:
        report.add("code_file", f"should be {code_file!r}, found {declared_code!r}")
    code_path = Path(kernel_dir) / (declared_code or code_file)
    if not code_path.is_file():
        report.add("code_file", f"{code_path} does not exist")

    if str(metadata.get("language") or "").lower() not in ("python", ""):
        report.add("language", f"expected 'python', found {metadata.get('language')!r}")
    if str(metadata.get("kernel_type") or "").lower() not in ("script", ""):
        report.add(
            "kernel_type",
            f"expected 'script' (the bridge is a script kernel), found {metadata.get('kernel_type')!r}",
        )

    # -- privacy -----------------------------------------------------------
    if not _is_true(metadata.get("is_private")):
        report.add(
            "is_private",
            "must be true: the kernel handles private media and its output is public otherwise",
        )

    # -- accelerator -------------------------------------------------------
    if not _is_true(metadata.get("enable_gpu")):
        report.add("enable_gpu", "must be true for a GPU benchmark run")
    shape = str(metadata.get("machine_shape") or "")
    if shape != "NvidiaTeslaT4":
        report.add(
            "machine_shape",
            f"expected 'NvidiaTeslaT4', found {shape!r} "
            "(P100/CPU shapes change the numbers and have no NVENC)",
        )

    # -- internet ----------------------------------------------------------
    if "enable_internet" not in metadata:
        report.add(
            "enable_internet",
            "missing: the kernel pip-installs openai-whisper and yt-dlp at start, "
            "so the run needs internet even though nothing is uploaded",
        )
    elif require_internet and not _is_true(metadata.get("enable_internet")):
        report.add(
            "enable_internet",
            "must be true for Phase 1: the dataset does not bundle torch/whisper wheels, "
            "so the kernel installs requirements.txt at start",
        )

    # -- datasets ----------------------------------------------------------
    datasets = metadata.get("dataset_sources")
    if datasets is None:
        report.add("dataset_sources", "missing; expected two private datasets")
        datasets = []
    if isinstance(datasets, (str, bytes)) or not isinstance(datasets, Sequence):
        report.add("dataset_sources", "must be a list")
        datasets = []
    datasets = [str(item) for item in datasets]

    if len(datasets) != expected_datasets:
        report.add(
            "dataset_sources",
            f"expected exactly {expected_datasets} entries (code + private input), "
            f"found {len(datasets)}: {datasets}",
        )
    if len(set(datasets)) != len(datasets):
        report.add("dataset_sources", f"duplicate entries: {datasets}")
    for dataset in datasets:
        if _looks_like_placeholder(dataset):
            report.add("dataset_sources", f"placeholder username: {dataset!r}")
        elif not _KERNEL_ID_RE.match(dataset):
            report.add("dataset_sources", f"{dataset!r} is not '<owner>/<dataset-slug>'")
        else:
            owner, slug = dataset.split("/", 1)
            if not _SLUG_RE.match(slug):
                report.add(
                    "dataset_sources",
                    f"{dataset!r}: dataset slugs are lowercase letters, digits and hyphens",
                )
            if kernel_id.startswith(owner + "/") and slug == kernel_id.split("/", 1)[1]:
                report.add("dataset_sources", f"{dataset!r} is the kernel itself, not a dataset")
    if kernel_id and datasets and kernel_id.lower() in {d.lower() for d in datasets}:
        report.add("dataset_sources", "references the kernel's own id")

    # -- cosmetic warnings (become failures under --strict) ----------------
    if not str(metadata.get("title") or "").strip():
        report.add("title", "missing; Kaggle will fall back to the slug", "warning")
    owners = {dataset.split("/", 1)[0].lower() for dataset in datasets if "/" in dataset}
    kernel_owner = kernel_id.split("/", 1)[0].lower() if "/" in kernel_id else ""
    if kernel_owner and owners - {kernel_owner}:
        report.add(
            "dataset_sources",
            f"datasets owned by another account ({sorted(owners - {kernel_owner})}) — "
            "allowed only if those datasets are shared with you",
            "warning",
        )

    for name, expected in (
        ("kernel_sources", 0),
        ("competition_sources", 0),
    ):
        value = metadata.get(name)
        if value and len(value) != expected:
            report.add(name, f"unexpected entries: {value}")

    return report


def load_kernel_metadata(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except FileNotFoundError as error:
        raise PackagingError(f"missing kernel metadata: {path}") from error
    except json.JSONDecodeError as error:
        raise PackagingError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise PackagingError(f"{path} must contain a JSON object")
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _emit(payload: Dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return

    status = payload.get("status")
    if status == "ok":
        print(f"  ok   {payload.get('summary', '')}")
    elif status == "warn":
        print(f"  warn {payload.get('summary', '')}")
    else:
        print(f"  FAIL {payload.get('summary', '')}")
    for issue in payload.get("issues", []):
        print("       " + issue)
    for key, value in payload.get("details", {}).items():
        print(f"       {key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m processor.packaging", description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("limits", help="print the documented size limits")

    check = sub.add_parser("check-metadata", help="validate kernel-metadata.json")
    check.add_argument("--metadata", required=True)
    check.add_argument("--kernel-dir", required=True)
    check.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures (used by push.ps1)",
    )

    tree = sub.add_parser("zip-tree", help="zip a directory (ZIP64, no caches)")
    tree.add_argument("--source", required=True)
    tree.add_argument("--destination", required=True)
    tree.add_argument("--arc-root", default=None)

    inputs = sub.add_parser("zip-input", help="zip a video + job.json with a size guard")
    inputs.add_argument("--video", required=True)
    inputs.add_argument("--job", required=True)
    inputs.add_argument("--destination", required=True)
    inputs.add_argument("--max-gb", type=float, default=None)

    verify = sub.add_parser("verify-zip", help="re-open an archive and verify it")
    verify.add_argument("--path", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "limits":
            payload = {
                "status": "ok",
                "summary": "documented limits",
                "details": {
                    "max_input": human_bytes(DEFAULT_MAX_INPUT_BYTES),
                    "recommended_input": human_bytes(RECOMMENDED_MAX_INPUT_BYTES),
                    "kaggle_autosaved_output": human_bytes(KAGGLE_AUTOSAVE_BYTES),
                    "compress_archive_ceiling": human_bytes(COMPRESS_ARCHIVE_LIMIT_BYTES),
                    "zip64": "enabled (files above 4 GB are stored correctly)",
                },
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "check-metadata":
            metadata = load_kernel_metadata(Path(args.metadata))
            report = validate_kernel_metadata(metadata, kernel_dir=Path(args.kernel_dir))
            failed = bool(report.errors) or (args.strict and bool(report.warnings))
            payload = {
                "status": "fail" if failed else ("warn" if report.warnings else "ok"),
                "summary": f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)",
                "issues": [str(issue) for issue in report.issues],
                "details": {
                    "id": metadata.get("id"),
                    "machine_shape": metadata.get("machine_shape"),
                    "is_private": metadata.get("is_private"),
                    "enable_internet": metadata.get("enable_internet"),
                    "dataset_sources": ", ".join(str(d) for d in metadata.get("dataset_sources") or []),
                },
            }
            _emit(payload, as_json=args.json)
            return 0 if not failed else 1

        if args.command == "zip-tree":
            data = build_tree_zip(
                Path(args.source), Path(args.destination), arc_root=args.arc_root
            )
            payload = {
                "status": "ok",
                "summary": f"{args.destination} ({human_bytes(data['compressed_bytes'])}, "
                f"{data['entries']} entries)",
                "details": {
                    "sha256": data["sha256"],
                    "largest_entry": human_bytes(data["largest_entry_bytes"]),
                },
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "zip-input":
            video = Path(args.video)
            job = Path(args.job)
            if not video.is_file():
                raise PackagingError(f"video not found: {video}")
            if not job.is_file():
                raise PackagingError(f"job manifest not found: {job}")
            max_bytes = (
                int(args.max_gb * 1024**3) if args.max_gb else DEFAULT_MAX_INPUT_BYTES
            )
            warning = check_input_size(video.stat().st_size, max_bytes=max_bytes)
            data = build_file_zip({video.name: video, "job.json": job}, Path(args.destination))
            payload = {
                "status": "warn" if warning else "ok",
                "summary": f"{args.destination} ({human_bytes(data['compressed_bytes'])})",
                "issues": [warning] if warning else [],
                "details": {
                    "sha256": data["sha256"],
                    "entries": str(data["entries"]),
                    "zip64": str(data["uses_zip64"]),
                },
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "verify-zip":
            summary = zip_summary(Path(args.path))
            payload = {
                "status": "ok",
                "summary": f"{args.path} verified",
                "details": {k: str(v) for k, v in summary.as_dict().items() if k != "path"},
            }
            _emit(payload, as_json=args.json)
            return 0

    except PackagingError as error:
        print(f"  FAIL {error}", file=sys.stderr)
        return 1

    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
