#!/usr/bin/env python3
"""Verify the local AI stack from inside every service container.

Why this exists
---------------
Checking ``ollama list`` on the host, or calling the backend's ``/health``
endpoint, does not prove that a worker container can reach the model. Each
container has its own network namespace and its own mounts, so the render worker
can fail while the backend succeeds.

This script runs the same probe *inside* each service::

    docker compose exec -T backend   .venv/bin/python -m src.local_ai --json
    docker compose exec -T worker    .venv/bin/python -m src.local_ai --json
    docker compose exec -T ai-worker .venv/bin/python -m src.local_ai --json

and reports one table of component x service, naming the exact failing
component (``worker: ollama_reachable``), plus the host-side Ollama listener
check that explains the usual Windows cause.

Usage::

    python scripts/verify_local_ai.py
    python scripts/verify_local_ai.py --json
    python scripts/verify_local_ai.py --services backend worker

Exit code 0 only when every required component passes in every service that is
running. Nothing is downloaded or changed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("backend", "worker", "ai-worker")
PROBE = ["exec", "-T", "{service}", ".venv/bin/python", "-m", "src.local_ai", "--json"]
PROBE_TIMEOUT = 90

OK = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# ANSI is disabled when the output is piped, so CI logs stay readable.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def compose(*args: str, timeout: int = PROBE_TIMEOUT) -> subprocess.CompletedProcess[str] | None:
    """Run `docker compose <args>` from the repo root; None if Docker is absent."""
    command = ["docker", "compose", *args]
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def docker_available() -> bool:
    result = compose("version", timeout=30)
    return bool(result and result.returncode == 0)


def running_services() -> set[str]:
    result = compose("ps", "--services", "--status", "running", timeout=60)
    if not result or result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def probe_service(service: str) -> dict:
    """Run the in-container probe and return its parsed JSON report."""
    args = [part.format(service=service) for part in PROBE]
    result = compose(*args)
    if result is None:
        return {"service": service, "ready": False, "error": "docker compose is not available"}
    if result.returncode == 0 or result.stdout.strip().startswith("{"):
        try:
            payload = json.loads(result.stdout[result.stdout.index("{"):])
            payload["service"] = service
            return payload
        except (ValueError, json.JSONDecodeError):
            pass
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = " / ".join(detail[-3:]) if detail else f"exit code {result.returncode}"
    return {
        "service": service,
        "ready": False,
        "error": f"probe failed inside the container: {tail}",
        "components": [],
    }


def host_ollama_report() -> dict:
    """What the *host* sees, used only to explain container-side failures."""
    report: dict = {"installed": False, "version": None, "models": [], "loopback_answers": False}

    try:
        version = subprocess.run(
            ["ollama", "--version"], capture_output=True, text=True, timeout=15, check=False
        )
        if version.returncode == 0:
            report["installed"] = True
            report["version"] = version.stdout.strip().splitlines()[0] if version.stdout.strip() else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if report["installed"]:
        try:
            listed = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=60, check=False
            )
            if listed.returncode == 0:
                lines = [line for line in listed.stdout.splitlines()[1:] if line.strip()]
                report["models"] = [line.split()[0] for line in lines]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Does anything answer on the host loopback? If yes but containers fail, the
    # daemon is bound to 127.0.0.1 and cannot be reached from Docker.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            report["loopback_answers"] = sock.connect_ex(("127.0.0.1", 11434)) == 0
        except OSError:
            report["loopback_answers"] = False

    report["bind_hint"] = (
        "Ollama answers on the host loopback (127.0.0.1:11434). Containers cannot "
        "reach a loopback-only listener: set OLLAMA_HOST=0.0.0.0:11434, restart "
        "Ollama, and allow TCP 11434 from the Docker/WSL subnet only. See "
        "docs/local-ai.md and scripts/setup-ollama-windows.ps1."
        if report["loopback_answers"]
        else None
    )
    return report


def collect(paths: list[str]) -> list[dict]:
    reports = []
    for service in paths:
        reports.append(probe_service(service))
    return reports


def render(reports: list[dict], host: dict, skipped: list[str]) -> str:
    rows: dict[str, dict] = {}
    order: list[str] = []
    for report in reports:
        for component in report.get("components", []):
            cid = component["id"]
            if cid not in rows:
                rows[cid] = {"label": component["label"], "results": {}, "required": component.get("required", True)}
                order.append(cid)
            rows[cid]["results"][report["service"]] = (
                OK if component["ok"] else (FAIL if component.get("required", True) else SKIP)
            )

    services = [report["service"] for report in reports]
    label_width = max([len(row["label"]) for row in rows.values()] + [len("Component")])
    primary_width = max([len(service) for service in services] + [len("Component")])

    lines = ["", paint("ClipWright Studio — local AI verification (from inside each container)", "1")]
    lines.append("=" * (label_width + 13 * len(services) + 20))
    header = "Component".ljust(label_width) + "".join(f"  {service.center(9)}" for service in services)
    lines.append(header)
    lines.append("-" * (label_width + 13 * len(services) + 20))
    for cid in order:
        row = rows[cid]
        cells = "".join(
            f"  {(paint(row['results'][service], '32') if row['results'][service] == OK else paint(row['results'][service], '31' if row['results'][service] == FAIL else '33')).center(9)}"
            if service in row["results"]
            else f"  {'-'.center(9)}"
            for service in services
        )
        lines.append(row["label"].ljust(label_width) + cells)

    failing = []
    for report in reports:
        for component in report.get("components", []):
            if component.get("required", True) and not component["ok"]:
                failing.append(f"{report['service']}: {component['id']} — {component['detail']}")
        if report.get("error"):
            failing.append(f"{report['service']}: {report['error']}")

    for service in skipped:
        lines.append("")
        lines.append(f"{paint('SKIP', '33')} {service} is not running; start it with "
                     f"`docker compose up -d {service}` and re-run.")

    lines.append("")
    lines.append(paint("Host-side Ollama", "1"))
    lines.append(
        f"  installed: {'yes' if host['installed'] else 'no'}"
        + (f" ({host['version']})" if host.get("version") else "")
    )
    lines.append(f"  models:    {', '.join(host['models']) if host['models'] else 'none'}")
    lines.append(f"  loopback 127.0.0.1:11434 answers: {'yes' if host['loopback_answers'] else 'no'}")

    if failing:
        lines.append("")
        lines.append(paint("Not ready:", "1;31"))
        for entry in failing:
            lines.append(f"  - {entry}")
        if host.get("bind_hint") and any("ollama" in entry for entry in failing):
            lines.append("")
            lines.append(paint(host["bind_hint"], "33"))
    else:
        lines.append("")
        lines.append(paint("All running services can transcribe, analyse and render locally.", "1;32"))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the local AI stack from inside the backend, worker and ai-worker."
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--services", nargs="+", default=list(SERVICES), help="services to probe")
    args = parser.parse_args(argv)

    if not docker_available():
        message = (
            "Docker Compose is not available here. Container-side verification "
            "requires the stack: run `docker compose up -d` first, or run the same "
            "probe inside a container with "
            "`.venv/bin/python -m src.local_ai --check`."
        )
        if args.json:
            print(json.dumps({"ready": False, "error": message}, indent=2))
        else:
            print(paint(message, "33"))
        return 2

    running = running_services()
    requested = list(args.services)
    skipped = [service for service in requested if service not in running]
    reports = collect([service for service in requested if service in running])
    host = host_ollama_report()

    if args.json:
        print(json.dumps({"ready": all(report.get("ready") for report in reports) and not skipped,
                          "services": reports, "skipped": skipped, "host": host}, indent=2))
    else:
        print(render(reports, host, skipped))

    if skipped:
        return 1
    return 0 if all(report.get("ready") for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
