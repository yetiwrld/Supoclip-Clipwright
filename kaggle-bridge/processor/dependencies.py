"""Dependency bootstrap: prove the kernel has what it needs, install only what it lacks.

A `requirements.txt` sitting in an attached dataset installs nothing. Kaggle runs
the notebook; nobody runs `pip install -r`. So the kernel does it itself, under
three rules:

1. **Never touch PyTorch.** Kaggle's image ships a CUDA build. `pip install torch`
   (or a stray `-r requirements.txt` that lists it) can silently replace it with a
   CPU wheel, after which every "GPU" timing is a lie. torch is therefore checked,
   never installed, and its CUDA build is compared before and after any install.
2. **Install only what is missing.** Installed and importable means untouched;
   this keeps the bootstrap to a couple of seconds on a warm image.
3. **Verify afterwards.** CUDA, Whisper provenance, the model libraries and the
   ffmpeg pair are re-checked *after* installation, because "pip said ok" is not a
   verified GPU.

Install output can contain an index URL with an embedded token, so every line shown
or stored passes through :func:`processor.kaggle_cli.redact` first.

Nothing here is importable-expensive: torch and whisper are probed through
callables that tests replace, so the whole bootstrap is unit-tested without a GPU.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from processor import kaggle_cli
from processor.validation import Check

# --------------------------------------------------------------------------- #
# what the pipeline actually needs
# --------------------------------------------------------------------------- #

#: Packages the Kaggle image is expected to provide. Never installed here.
PROVIDED_PACKAGES = frozenset(
    {
        "torch",
        "torchvision",
        "torchaudio",
        "nvidia-cuda-runtime-cu12",
        "triton",
    }
)

#: Fallback specs, used when no requirements.txt can be found in the mounts.
BUILTIN_SPECS: Tuple[str, ...] = (
    "openai-whisper==20250625",
    "yt-dlp>=2025.9.5",
    "transformers>=4.46",
    "accelerate>=1.0",
    "sentencepiece>=0.2",
)

#: Distribution -> import name, where they differ.
_IMPORT_NAMES = {
    "openai-whisper": "whisper",
    "yt-dlp": "yt_dlp",
    "sentencepiece": "sentencepiece",
    "transformers": "transformers",
    "accelerate": "accelerate",
    "torch": "torch",
}

#: Modules the pipeline imports at runtime, with the reason they matter.
MODULE_PURPOSE = {
    "torch": "CUDA build from the Kaggle image (never installed by this bridge)",
    "whisper": "Gate B transcription — must be openai-whisper, not the legacy package",
    "transformers": "Gate C fallback backend",
    "accelerate": "Gate C fallback backend (device placement)",
    "sentencepiece": "Gate C tokenizer for Llama-3.2",
    "yt_dlp": "Gate E downloads",
}

#: Binaries, probed with their version flag.
REQUIRED_BINARIES: Tuple[str, ...] = ("ffmpeg", "ffprobe")

DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800
_LOG_TAIL_CHARS = 4000


# --------------------------------------------------------------------------- #
# requirement parsing
# --------------------------------------------------------------------------- #

_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?\s*(?P<spec>.*)$"
)
_OPERATOR_RE = re.compile(r"(==|>=|<=|!=|~=|>|<)\s*([0-9A-Za-z][0-9A-Za-z.+!_-]*)")


@dataclass(frozen=True)
class Requirement:
    """One parsed ``requirements.txt`` line."""

    raw: str
    name: str
    display_name: str
    specifiers: Tuple[Tuple[str, str], ...] = ()

    @property
    def canonical(self) -> str:
        """Lowercase distribution name, per PEP 503."""
        return re.sub(r"[-_.]+", "-", self.name).lower()

    @property
    def import_name(self) -> str:
        return _IMPORT_NAMES.get(self.canonical, self.canonical.replace("-", "_"))

    @property
    def is_provided(self) -> bool:
        return self.canonical in {re.sub(r"[-_.]+", "-", p).lower() for p in PROVIDED_PACKAGES}

    def version_ok(self, version: Optional[str]) -> bool:
        if not self.specifiers:
            return version is not None
        if version is None:
            return False
        return all(_compare(version, operator, target) for operator, target in self.specifiers)


def parse_requirements(text: str) -> List[Requirement]:
    """Parse requirement lines, ignoring comments, blanks, flags and includes."""
    parsed: List[Requirement] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "http://", "https://", "git+", "file:")):
            continue  # -r/-c/--index-url/direct URLs are not packages we manage
        match = _REQUIREMENT_RE.match(stripped)
        if not match:
            continue
        name = match.group("name")
        spec_text = (match.group("spec") or "").strip()
        specifiers = tuple(
            (operator, target) for operator, target in _OPERATOR_RE.findall(spec_text)
        )
        if spec_text and not specifiers:
            continue  # an unparseable spec is safer to skip than to mis-enforce
        parsed.append(
            Requirement(
                raw=stripped,
                name=name,
                display_name=stripped,
                specifiers=specifiers,
            )
        )
    return parsed


def _version_tuple(version: str) -> Tuple[int, ...]:
    numbers: List[int] = []
    for part in re.split(r"[.\-+!]", str(version)):
        digits = re.match(r"^(\d+)", part)
        if not digits:
            break
        numbers.append(int(digits.group(1)))
    return tuple(numbers)


def _padded(left: Tuple[int, ...], right: Tuple[int, ...]) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    width = max(len(left), len(right))
    return (
        left + (0,) * (width - len(left)),
        right + (0,) * (width - len(right)),
    )


def _compare(version: str, operator: str, target: str) -> bool:
    left, right = _padded(_version_tuple(version), _version_tuple(target))
    if not right:
        return True  # unparseable constraint: do not block on it
    if operator == "==":
        return left[: len(right)] == right
    if operator == "!=":
        return left[: len(right)] != right
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    if operator == "~=":  # compatible release: >= target, same leading segments
        return left >= right and left[: max(len(right) - 1, 1)] == right[: max(len(right) - 1, 1)]
    return True


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def find_requirements(
    *,
    search_roots: Sequence[os.PathLike | str] = (),
    max_depth: int = 3,
) -> Optional[Path]:
    """Find the attached ``requirements.txt``, preferring the shallowest one."""
    seen: List[Path] = []
    for raw_root in search_roots:
        root = Path(raw_root)
        if not root.exists():
            continue
        base_depth = len(root.parts)
        for current, directories, files in os.walk(root, followlinks=False):
            path = Path(current)
            depth = len(path.parts) - base_depth
            directories[:] = sorted(
                d for d in directories if d not in ("__pycache__", ".git", ".ipynb_checkpoints")
            )
            if "requirements.txt" in files:
                candidate = path / "requirements.txt"
                if candidate not in seen:
                    return candidate
            if depth >= max_depth:
                directories[:] = []
    return None


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #


def module_available(name: str) -> bool:
    """True when a module can be imported, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def distribution_version(distribution: str) -> Optional[str]:
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover - environment specific
        return None


def ffmpeg_pair() -> Dict[str, Any]:
    """ffmpeg and ffprobe, as executables with a version string."""
    report: Dict[str, Any] = {"ffmpeg": None, "ffprobe": None, "versions": {}}
    for binary in REQUIRED_BINARIES:
        report[binary] = shutil.which(binary)
        if not report[binary]:
            continue
        try:
            completed = subprocess.run(
                [binary, "-version"], capture_output=True, text=True, timeout=30, check=False
            )
            first = (completed.stdout or completed.stderr or "").splitlines()
            report["versions"][binary] = first[0].strip() if first else None
        except (OSError, subprocess.SubprocessError):
            report["versions"][binary] = None
    return report


@dataclass
class Probes:
    """Injectable probes so the bootstrap is testable without a GPU."""

    torch: Callable[[], Dict[str, Any]]
    whisper: Callable[[], Dict[str, Any]]
    import_available: Callable[[str], bool] = module_available
    distribution: Callable[[str], Optional[str]] = distribution_version
    binaries: Callable[[], Dict[str, Any]] = ffmpeg_pair


def default_probes() -> Probes:
    from processor import environment, transcription

    return Probes(
        torch=environment.torch_report,
        whisper=transcription.whisper_environment,
    )


# --------------------------------------------------------------------------- #
# state + planning
# --------------------------------------------------------------------------- #


@dataclass
class DependencyState:
    requirement: Requirement
    installed_version: Optional[str]
    importable: bool
    ok: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "package": self.requirement.display_name,
            "import_name": self.requirement.import_name,
            "installed_version": self.installed_version,
            "importable": self.importable,
            "ok": self.ok,
            "provided_by_image": self.requirement.is_provided,
            "detail": self.detail,
        }


def inspect(requirements: Sequence[Requirement], probes: Probes) -> List[DependencyState]:
    states: List[DependencyState] = []
    for requirement in requirements:
        version = probes.distribution(requirement.canonical)
        importable = probes.import_available(requirement.import_name)
        version_ok = requirement.version_ok(version)
        ok = importable and version_ok
        detail = ""
        if not importable:
            detail = f"module {requirement.import_name!r} is not importable"
        elif not version_ok:
            detail = f"installed {version!r} does not satisfy {requirement.display_name}"
        states.append(DependencyState(requirement, version, importable, ok, detail))
    return states


def install_plan(states: Sequence[DependencyState]) -> Tuple[List[str], List[str]]:
    """Split unsatisfied requirements into (installable, refused).

    ``refused`` holds image-provided packages such as torch: installing one would
    replace the CUDA build the whole benchmark depends on.
    """
    installable: List[str] = []
    refused: List[str] = []
    for state in states:
        if state.ok:
            continue
        if state.requirement.is_provided:
            refused.append(state.requirement.canonical)
        else:
            installable.append(state.requirement.raw)
    return installable, refused


# --------------------------------------------------------------------------- #
# installing
# --------------------------------------------------------------------------- #

InstallRunner = Callable[[Sequence[str], int, Mapping[str, str]], Tuple[int, str, str]]


def _default_runner(command: Sequence[str], timeout: int, env: Mapping[str, str]) -> Tuple[int, str, str]:
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env),
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def redact_log(text: str, *, tail_chars: int = _LOG_TAIL_CHARS) -> str:
    """Redact credentials and keep only the tail of pip's output."""
    if not text:
        return ""
    cleaned = kaggle_cli.redact(text)
    if len(cleaned) > tail_chars:
        cleaned = "...\n" + cleaned[-tail_chars:]
    return cleaned


@dataclass
class InstallResult:
    attempted: bool = False
    ok: bool = True
    command: Tuple[str, ...] = ()
    returncode: Optional[int] = None
    seconds: float = 0.0
    log_tail: str = ""
    packages: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "ok": self.ok,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 2),
            "packages": list(self.packages),
            "command": " ".join(self.command),
            "log_tail": self.log_tail,
        }


def pip_install(
    packages: Sequence[str],
    *,
    python: Optional[str] = None,
    runner: Optional[InstallRunner] = None,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    dry_run: bool = False,
) -> InstallResult:
    """Install the given specs, never touching image-provided packages."""
    packages = [package for package in packages if package.strip()]
    if not packages:
        return InstallResult(attempted=False, ok=True, packages=())

    blocked = [
        package
        for package in packages
        if re.sub(r"[-_.]+", "-", _REQUIREMENT_RE.match(package).group("name")).lower()  # type: ignore[union-attr]
        in {re.sub(r"[-_.]+", "-", p).lower() for p in PROVIDED_PACKAGES}
    ]
    if blocked:
        raise DependencyError(
            "refusing to pip-install image-provided package(s) "
            + ", ".join(blocked)
            + ": replacing Kaggle's CUDA PyTorch would invalidate every GPU measurement"
        )

    command = (
        python or sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--no-cache-dir",
        # Never upgrade an already-satisfied dependency (in particular torch).
        "--upgrade-strategy",
        "only-if-needed",
        *packages,
    )
    if dry_run:
        return InstallResult(attempted=False, ok=True, command=command, packages=tuple(packages))

    env = {
        **os.environ,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    run = runner or _default_runner
    started = time.time()
    try:
        returncode, stdout, stderr = run(command, timeout, env)
    except FileNotFoundError:
        return InstallResult(
            attempted=True,
            ok=False,
            command=command,
            returncode=None,
            seconds=time.time() - started,
            log_tail=f"python executable not found: {command[0]}",
            packages=tuple(packages),
        )
    except subprocess.TimeoutExpired:
        return InstallResult(
            attempted=True,
            ok=False,
            command=command,
            returncode=None,
            seconds=time.time() - started,
            log_tail=f"pip did not finish within {timeout}s",
            packages=tuple(packages),
        )
    return InstallResult(
        attempted=True,
        ok=returncode == 0,
        command=command,
        returncode=returncode,
        seconds=time.time() - started,
        log_tail=redact_log(f"{stdout}\n{stderr}".strip()) if returncode != 0 else redact_log(stdout),
        packages=tuple(packages),
    )


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


class DependencyError(Exception):
    """Raised when the environment cannot be prepared safely."""


@dataclass
class DependencyReport:
    requirements_source: Optional[str] = None
    requested: Tuple[str, ...] = ()
    states: List[DependencyState] = field(default_factory=list)
    install: InstallResult = field(default_factory=InstallResult)
    checks: List[Check] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    torch_before: Dict[str, Any] = field(default_factory=dict)
    torch_after: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "requirements_source": self.requirements_source,
            "requested": list(self.requested),
            "dependencies": [state.as_dict() for state in self.states],
            "install": self.install.as_dict(),
            "checks": [
                {"name": check.name, "ok": check.ok, "severity": check.severity, "detail": check.detail}
                for check in self.checks
            ],
            "problems": self.problems,
            "torch": {"before": self.torch_before, "after": self.torch_after},
        }

    def to_markdown(self) -> str:
        lines = ["## Dependency bootstrap", ""]
        lines.append(f"- requirements source: {self.requirements_source or 'built-in defaults'}")
        if self.skipped:
            lines.append("- **skipped** (`--skip-dependency-bootstrap`)")
        if self.install.attempted:
            state = "ok" if self.install.ok else f"FAILED (rc {self.install.returncode})"
            lines.append(
                f"- installed {', '.join(self.install.packages)} in {self.install.seconds:.1f}s: {state}"
            )
        elif self.install.packages:
            lines.append(f"- nothing to install ({len(self.requested)} requirement(s) satisfied)")
        lines.append("")
        lines.append("| Package | Installed | Importable | OK |")
        lines.append("|---|---|---|---|")
        for state in self.states:
            lines.append(
                f"| `{state.requirement.display_name}` | {state.installed_version or '—'} "
                f"| {state.importable} | {'yes' if state.ok else 'NO'} |"
            )
        lines.append("")
        lines.append("| Check | Result | Detail |")
        lines.append("|---|---|---|")
        for check in self.checks:
            lines.append(f"| {check.name} | {'ok' if check.ok else 'FAIL'} | {check.detail} |")
        if self.problems:
            lines += ["", "**Problems**", ""] + [f"- {problem}" for problem in self.problems]
        return "\n".join(lines) + "\n"


def verify_environment(
    *,
    gpu_required: bool,
    probes: Optional[Probes] = None,
    require_ffmpeg: bool = True,
) -> List[Check]:
    """Re-check CUDA, Whisper provenance, the model libraries and ffmpeg."""
    probes = probes or default_probes()
    checks: List[Check] = []

    torch_info = probes.torch()
    cuda_build = torch_info.get("cuda_build") if isinstance(torch_info, Mapping) else None
    cuda_available = bool(torch_info.get("cuda_available")) if isinstance(torch_info, Mapping) else False
    device_count = int(torch_info.get("device_count") or 0) if isinstance(torch_info, Mapping) else 0

    if gpu_required:
        checks.append(
            Check(
                "torch_cuda_build",
                bool(cuda_build),
                f"torch {torch_info.get('version')} built for CUDA {cuda_build}"
                if isinstance(torch_info, Mapping)
                else "torch not installed",
            )
        )
        checks.append(
            Check(
                "cuda_devices",
                cuda_available and device_count > 0,
                f"cuda_available={cuda_available}, device_count={device_count}",
            )
        )
    else:
        checks.append(
            Check("torch", bool(torch_info.get("installed")) if isinstance(torch_info, Mapping) else False,
                  f"torch {torch_info.get('version')} (GPU not required for this job)")
        )

    whisper_info = probes.whisper()
    module_path = (whisper_info or {}).get("module_path") or ""
    providers = sorted((whisper_info or {}).get("distributions") or [])
    whisper_ok = bool((whisper_info or {}).get("importable")) and bool(
        (whisper_info or {}).get("has_load_model")
    )
    checks.append(
        Check(
            "whisper_importable",
            whisper_ok,
            f"{module_path or 'not importable'}"
            + (f" (providers: {', '.join(providers)})" if providers else ""),
        )
    )
    checks.append(
        Check(
            "whisper_is_openai_whisper",
            "openai_whisper" in module_path or "openai-whisper" in providers,
            "the imported 'whisper' module must come from openai-whisper; "
            f"module={module_path or 'n/a'}, providers={providers or 'unknown'}",
        )
    )
    checks.append(
        Check(
            "whisper_single_provider",
            len(providers) <= 1,
            f"providers: {', '.join(providers) or 'unknown'}",
        )
    )

    for requirement in parse_requirements("\n".join(BUILTIN_SPECS)):
        if requirement.canonical == "openai-whisper":
            continue  # covered above, with provenance
        available = probes.import_available(requirement.import_name)
        version = probes.distribution(requirement.canonical)
        checks.append(
            Check(
                f"import_{requirement.import_name}",
                available,
                MODULE_PURPOSE.get(requirement.import_name, "") + (f" (version {version})" if version else ""),
            )
        )

    if require_ffmpeg:
        binaries = probes.binaries()
        for binary in REQUIRED_BINARIES:
            path = binaries.get(binary)
            checks.append(
                Check(
                    f"binary_{binary}",
                    bool(path),
                    (binaries.get("versions", {}) or {}).get(binary) or (str(path) if path else "not on PATH"),
                )
            )
    return checks


def bootstrap(
    *,
    gpu_required: bool,
    search_roots: Sequence[os.PathLike | str] = (),
    requirements_path: Optional[os.PathLike | str] = None,
    install: bool = True,
    dry_run: bool = False,
    probes: Optional[Probes] = None,
    runner: Optional[InstallRunner] = None,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    require_ffmpeg: bool = True,
) -> DependencyReport:
    """Discover, install what is missing, then verify the result."""
    probes = probes or default_probes()
    report = DependencyReport()

    path: Optional[Path] = Path(requirements_path) if requirements_path else None
    if path is None:
        path = find_requirements(search_roots=search_roots)
    if path and path.is_file():
        report.requirements_source = str(path)
        text = path.read_text(encoding="utf-8-sig")
        requirements = parse_requirements(text)
        if not requirements:
            requirements = parse_requirements("\n".join(BUILTIN_SPECS))
    else:
        report.requirements_source = None
        requirements = parse_requirements("\n".join(BUILTIN_SPECS))

    report.requested = tuple(requirement.display_name for requirement in requirements)
    report.torch_before = probes.torch()

    report.states = inspect(requirements, probes)
    installable, refused = install_plan(report.states)

    if refused:
        report.problems.append(
            "the image was expected to provide "
            + ", ".join(sorted(set(refused)))
            + ", but it is missing or broken. Refusing to pip-install it: a wheel from "
            "PyPI would not be the CUDA build this benchmark needs. Fix the notebook "
            "image (or the accelerator setting) instead of installing torch."
        )

    if installable and install:
        report.install = pip_install(installable, runner=runner, timeout=timeout, dry_run=dry_run)
        if report.install.attempted and not report.install.ok:
            report.problems.append(
                "pip install failed for "
                + ", ".join(installable)
                + f" (exit {report.install.returncode}); see dependencies.json"
            )
        report.states = inspect(requirements, probes)
    elif installable:
        report.install = InstallResult(
            attempted=False, ok=True, packages=tuple(installable), log_tail="installation disabled"
        )
        report.problems.append(
            "missing packages and installation is disabled: " + ", ".join(installable)
        )

    torch_after = probes.torch()
    report.torch_after = torch_after
    before_cuda = report.torch_before.get("cuda_build") if isinstance(report.torch_before, Mapping) else None
    after_cuda = torch_after.get("cuda_build") if isinstance(torch_after, Mapping) else None
    if report.install.attempted and before_cuda and not after_cuda:
        report.problems.append(
            "torch's CUDA build disappeared during installation "
            f"(before: CUDA {before_cuda}, after: {after_cuda}). The GPU image was replaced; "
            "restart the kernel from a clean image."
        )

    report.checks = verify_environment(
        gpu_required=gpu_required, probes=probes, require_ffmpeg=require_ffmpeg
    )
    for check in report.checks:
        if not check.ok and check.severity == "error":
            report.problems.append(f"{check.name}: {check.detail}")

    still_missing = [state for state in report.states if not state.ok]
    for state in still_missing:
        message = f"{state.requirement.display_name} is still unavailable — {state.detail}"
        if message not in report.problems:
            report.problems.append(message)

    return report


def write_report(path: Path, report: DependencyReport) -> Path:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path
