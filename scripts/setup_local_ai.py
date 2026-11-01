#!/usr/bin/env python3
"""ClipWright Studio first-time local AI setup.

Standard library only, so it runs on a fresh Windows 11 machine before any
Python packages are installed.

What it checks
  1. Python, Docker, FFmpeg, Ollama
  2. Whether Ollama is running and whether the configured model is pulled
  3. Whether the Whisper checkpoint is on disk
  4. The backend's /local-ai/status when the stack is up

What it changes (only when you ask for it)
  * downloads the Whisper checkpoint into persistent local storage
  * pulls the Ollama model
  * writes the local-AI defaults into .env (never overwriting values you set)

Nothing here is downloaded unless you pass the matching flag, so opening the
app later never surprises you with a model download.

Examples (Windows 11, PowerShell):
    python scripts\\setup_local_ai.py --check
    python scripts\\setup_local_ai.py --install-whisper --pull-ollama-model --write-env
    python scripts\\setup_local_ai.py --install-whisper --whisper-model base
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WHISPER_MODELS_FILE = REPO_ROOT / "backend" / "src" / "whisper_models.py"

ENV_DEFAULTS = {
    "OFFLINE_MODE": "true",
    "ALLOW_HOSTED_AI": "false",
    "TRANSCRIPTION_PROVIDER": "whisper",
    "WHISPER_MODEL_SIZE": "small",
    "WHISPER_DEVICE": "cpu",
    "MODELS_DIR": "./data/models",
    "WHISPER_MODEL_DIR": "./data/models/whisper",
    "LLM": "ollama:llama3.2:3b",
    "AI_EDIT_MODEL": "ollama:llama3.2:3b",
}

OK = "[ ok ]"
WARN = "[warn]"
FAIL = "[fail]"


def load_whisper_models():
    """Import backend/src/whisper_models.py without installing the backend."""
    spec = importlib.util.spec_from_file_location("cw_whisper_models", WHISPER_MODELS_FILE)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SystemExit(f"Cannot load {WHISPER_MODELS_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_defaults(path: Path, defaults: dict[str, str]) -> list[str]:
    """Add missing keys, keep every existing value untouched."""
    existing = read_env_file(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not lines:
        example = REPO_ROOT / ".env.example"
        if example.exists():
            lines = example.read_text(encoding="utf-8").splitlines()
            existing = read_env_file(example)
    added: list[str] = []
    for key, value in defaults.items():
        if key in existing and existing[key]:
            continue
        lines.append(f"{key}={value}")
        added.append(key)
    if added:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def resolve_dir(value: str | None, fallback: str) -> Path:
    raw = (value or "").strip() or fallback
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_python() -> tuple[bool, str]:
    version = platform.python_version()
    return sys.version_info >= (3, 11), f"Python {version}"


def check_command(name: str, version_args: tuple[str, ...] = ("--version",)) -> tuple[bool, str]:
    path = shutil.which(name)
    if not path:
        return False, f"{name} not found on PATH"
    try:
        out = subprocess.run(
            [path, *version_args], capture_output=True, text=True, timeout=20
        )
        first = (out.stdout or out.stderr).strip().splitlines()
        return True, f"{name}: {first[0] if first else path}"
    except Exception as exc:  # pragma: no cover - host dependent
        return True, f"{name}: {path} ({exc})"


def check_docker() -> tuple[bool, str]:
    ok, detail = check_command("docker")
    if not ok:
        return False, "Docker not found. Install Docker Desktop to run the stack."
    try:
        out = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=30
        )
        if out.returncode != 0:
            return False, "docker compose plugin is missing"
        return True, detail + " (compose available)"
    except Exception as exc:  # pragma: no cover - host dependent
        return False, f"Docker compose unavailable: {exc}"


def ollama_native_url() -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base or "http://localhost:11434"


def check_ollama() -> tuple[bool, str, list[str]]:
    url = ollama_native_url()
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [
            str(item.get("name") or item.get("model") or "").strip()
            for item in payload.get("models") or []
            if isinstance(item, dict)
        ]
        return True, f"Ollama reachable at {url}", [m for m in models if m]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return False, f"Ollama not reachable at {url} ({exc})", []


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def whisper_status(model_name: str, directory: Path, models) -> tuple[bool, str]:
    canonical = models.normalize_model_name(model_name)
    path = models.checkpoint_path(canonical, directory)
    if not path.is_file() or path.stat().st_size == 0:
        return False, f"Whisper '{canonical}' missing at {path}"
    size_mb = path.stat().st_size / (1024 * 1024)
    return True, f"Whisper '{canonical}' present at {path} ({size_mb:.0f} MB)"


def download_whisper(model_name: str, directory: Path, models) -> bool:
    canonical = models.normalize_model_name(model_name)
    url = models.model_url(canonical)
    if not url:
        print(f"{FAIL}Unknown Whisper model '{model_name}'. Known: "
              f"{', '.join(sorted(models.WHISPER_MODEL_URLS))}")
        return False
    directory.mkdir(parents=True, exist_ok=True)
    target = models.checkpoint_path(canonical, directory)
    expected = models.expected_sha256(canonical)
    expected_mb = models.WHISPER_MODEL_SIZES_MB.get(canonical)

    if target.is_file() and target.stat().st_size > 0:
        if expected and sha256_of(target) == expected:
            print(f"{OK} Whisper '{canonical}' already downloaded at {target}")
            return True
        print(f"{WARN}Existing {target} failed its checksum; re-downloading")

    print(f"Downloading Whisper '{canonical}'"
          f"{f' (~{expected_mb} MB)' if expected_mb else ''} -> {target}")
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r  {pct:3d}%  {done // (1024 * 1024)} / {total // (1024 * 1024)} MB",
                          end="", flush=True)
        print()
        if expected and sha256_of(tmp) != expected:
            tmp.unlink(missing_ok=True)
            print(f"{FAIL}Checksum mismatch for {canonical}; download rejected")
            return False
        tmp.replace(target)
        print(f"{OK} Whisper '{canonical}' installed at {target}")
        return True
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        print(f"{FAIL}Download failed: {exc}")
        print("     Check your connection and re-run with --install-whisper.")
        return False


def pull_ollama_model(model: str) -> bool:
    binary = shutil.which("ollama")
    if not binary:
        print(f"{FAIL}The `ollama` command was not found. Install it from "
              "https://ollama.com/download and try again.")
        return False
    print(f"Pulling Ollama model '{model}' (one-time download)...")
    result = subprocess.run([binary, "pull", model])
    if result.returncode != 0:
        print(f"{FAIL}ollama pull {model} failed")
        return False
    print(f"{OK} Ollama model '{model}' installed")
    return True


def check_backend_status() -> tuple[bool, str]:
    base = os.environ.get("NEXT_PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as response:
            return response.status == 200, f"Backend responded at {base}/health"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"Backend not reachable at {base} ({exc})"


def run_container_verification() -> tuple[bool | None, str]:
    """Run scripts/verify_local_ai.py and summarize it.

    Returns (ok, detail) where ok is None when the stack is not running, which
    must never be reported as "ready".
    """
    script = REPO_ROOT / "scripts" / "verify_local_ai.py"
    if not script.exists():
        return None, "scripts/verify_local_ai.py is missing; cannot verify the containers"
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"container verification could not run ({exc})"
    if result.returncode == 2:
        return None, "Docker Compose is not running; containers were not verified"
    try:
        payload = json.loads(result.stdout[result.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return False, "container verification produced no readable report"
    if payload.get("skipped"):
        return False, f"services not running: {', '.join(payload['skipped'])}"
    failing = [
        f"{report['service']}:{component['id']}"
        for report in payload.get("services", [])
        for component in report.get("components", [])
        if component.get("required", True) and not component.get("ok")
    ]
    if failing:
        return False, "failing inside containers: " + ", ".join(failing)
    if payload.get("ready"):
        return True, "backend, worker and ai-worker can all reach Ollama and read Whisper"
    return False, "one or more containers are not ready"


def main(argv: list[str] | None = None) -> int:
    models = load_whisper_models()
    env_path = REPO_ROOT / ".env"
    env_values = read_env_file(env_path)

    parser = argparse.ArgumentParser(
        description="ClipWright Studio local AI setup (offline-first)."
    )
    parser.add_argument("--check", action="store_true", help="only report status")
    parser.add_argument("--install-whisper", action="store_true",
                        help="download the Whisper checkpoint (explicit one-time action)")
    parser.add_argument("--pull-ollama-model", action="store_true",
                        help="download the configured Ollama model (explicit one-time action)")
    parser.add_argument("--write-env", action="store_true",
                        help="add local-AI defaults to .env without touching existing values")
    parser.add_argument("--whisper-model", default=None,
                        help="tiny | base | small | medium | large-v3 (default: from .env or small)")
    parser.add_argument("--ollama-model", default=None,
                        help="Ollama model id (default: from .env LLM or llama3.2:3b)")
    args = parser.parse_args(argv)

    whisper_model = (
        args.whisper_model
        or env_values.get("WHISPER_MODEL_SIZE")
        or env_values.get("WHISPER_MODEL")
        or "small"
    )
    llm = args.ollama_model or env_values.get("LLM") or "ollama:llama3.2:3b"
    ollama_model = llm.split(":", 1)[1] if llm.startswith("ollama:") else "llama3.2:3b"
    whisper_dir = resolve_dir(
        env_values.get("WHISPER_MODEL_DIR") or os.environ.get("WHISPER_MODEL_DIR"),
        "data/models/whisper",
    )

    print("ClipWright Studio - local AI setup")
    print("=" * 62)
    print(f"Repository : {REPO_ROOT}")
    print(f"Whisper dir: {whisper_dir}")
    print(f"Whisper md : {models.normalize_model_name(whisper_model)}")
    print(f"Llama model: {ollama_model}")
    print()

    problems = 0

    py_ok, py_detail = check_python()
    print(f"{OK if py_ok else WARN} {py_detail} (3.11+ recommended)")
    problems += 0 if py_ok else 1

    ffmpeg_ok, ffmpeg_detail = check_command("ffmpeg", ("-version",))
    print(f"{OK if ffmpeg_ok else WARN} {ffmpeg_detail.splitlines()[0] if ffmpeg_ok else ffmpeg_detail}")
    problems += 0 if ffmpeg_ok else 1

    docker_ok, docker_detail = check_docker()
    print(f"{OK if docker_ok else WARN} {docker_detail}")

    ollama_ok, ollama_detail, installed_models = check_ollama()
    ollama_bin_ok, ollama_bin_detail = check_command("ollama", ("--version",))
    print(f"{OK if ollama_bin_ok else WARN} {ollama_bin_detail}")
    print(f"{OK if ollama_ok else FAIL} {ollama_detail}")

    def normalized(name: str) -> str:
        name = name.strip().lower()
        return name if ":" in name else f"{name}:latest"

    model_installed = any(normalized(m) == normalized(ollama_model) for m in installed_models)
    print(f"{OK if model_installed else WARN} Ollama model '{ollama_model}': "
          f"{'installed' if model_installed else 'not installed'}")
    if installed_models:
        print(f"       installed models: {', '.join(installed_models)}")

    whisper_ok, whisper_detail = whisper_status(whisper_model, whisper_dir, models)
    print(f"{OK if whisper_ok else WARN} {whisper_detail}")

    backend_ok, backend_detail = check_backend_status()
    print(f"{OK if backend_ok else WARN} {backend_detail}")

    # ---- actions ---------------------------------------------------------
    if args.write_env:
        added = write_env_defaults(env_path, ENV_DEFAULTS)
        if added:
            print(f"\n{OK} .env updated with: {', '.join(added)}")
        else:
            print(f"\n{OK} .env already has local-AI defaults; nothing changed")

    if args.install_whisper:
        print()
        if not download_whisper(whisper_model, whisper_dir, models):
            problems += 1
        else:
            whisper_ok = True
            whisper_detail = whisper_status(whisper_model, whisper_dir, models)[1]

    if args.pull_ollama_model:
        print()
        if not ollama_ok:
            print(f"{WARN}Ollama is not running. Start it (or `ollama serve`) and re-run.")
            problems += 1
        elif not pull_ollama_model(ollama_model):
            problems += 1

    # ---- container verification ------------------------------------------
    # Host facts are not enough: the worker containers have their own network
    # namespace and mounts, so readiness is only claimed once every running
    # service passes the in-container probe.
    containers_ok, containers_detail = run_container_verification()
    symbol = {True: OK, False: FAIL, None: WARN}[containers_ok]
    print(f"{symbol} Containers: {containers_detail}")
    if containers_ok is None:
        print("       Start the stack (docker compose up -d), then run:")
        print("       python scripts/verify_local_ai.py")

    # ---- summary ---------------------------------------------------------
    host_ready = whisper_ok and model_installed and ffmpeg_ok
    ready = host_ready and containers_ok is True
    print()
    print("=" * 62)
    if ready:
        print(f"{OK} Local AI is ready. Transcription uses Whisper "
              f"'{models.normalize_model_name(whisper_model)}' and analysis uses "
              f"Ollama '{ollama_model}', and every service verified it from inside "
              f"its container.")
    elif host_ready and containers_ok is not True:
        print(f"{WARN} Host checks pass, but offline readiness is NOT proven: the "
              f"containers have not confirmed access to Whisper and Ollama.")
        print("       Run: docker compose up -d && python scripts/verify_local_ai.py")
    else:
        print(f"{WARN} Not ready for offline processing yet.")
        if not whisper_ok:
            print("       Run: python scripts/setup_local_ai.py --install-whisper")
        if not model_installed:
            print(f"       Run: ollama pull {ollama_model}")
        if not ffmpeg_ok:
            print("       Install FFmpeg and add it to PATH (Docker images already include it).")
        if containers_ok is False:
            print("       Run: python scripts/verify_local_ai.py   (names the failing component)")
    print("       Verify inside the app: Settings -> Offline & local AI.")
    if args.check or not (args.install_whisper or args.pull_ollama_model or args.write_env):
        print("       This was a check only; nothing was downloaded.")
    return 0 if (ready and problems == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
