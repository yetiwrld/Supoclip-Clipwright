"""`scripts/verify_local_ai.py` — the multi-container readiness gate.

The script is what the setup flow and the docs tell the user to run, so its
reporting contract is tested directly: a failing component must be named with its
service, and an unverified stack must never exit 0.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_local_ai.py"


@pytest.fixture(scope="module")
def verify_module():
    spec = importlib.util.spec_from_file_location("verify_local_ai", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def report(service: str, ollama_ok: bool, whisper_ok: bool = True) -> dict:
    components = [
        {"id": "whisper_checkpoint", "label": "Whisper checkpoint (small)", "ok": whisper_ok,
         "detail": "ok" if whisper_ok else "Whisper model 'small' is not installed", "required": True},
        {"id": "ollama_reachable", "label": "Ollama reachable from this container", "ok": ollama_ok,
         "detail": "Reached http://host.docker.internal:11434" if ollama_ok
         else "Ollama is not reachable at http://host.docker.internal:11434", "required": True},
        {"id": "offline_mode", "label": "Offline mode", "ok": True, "detail": "On", "required": False},
    ]
    return {
        "service": service,
        "ready": ollama_ok and whisper_ok,
        "components": components,
        "failing_components": [
            component["id"] for component in components if component["required"] and not component["ok"]
        ],
    }


HOST = {
    "installed": True,
    "version": "ollama version 0.5.0",
    "models": ["llama3.2:3b"],
    "loopback_answers": True,
    "bind_hint": (
        "Ollama answers on the host loopback (127.0.0.1:11434). Containers cannot "
        "reach a loopback-only listener: set OLLAMA_HOST=0.0.0.0:11434, restart "
        "Ollama, and allow TCP 11434 from the Docker/WSL subnet only."
    ),
}


def test_ready_report_names_every_service(verify_module):
    reports = [report("backend", True), report("worker", True), report("ai-worker", True)]

    rendered = verify_module.render(reports, HOST, [])

    assert "All running services" in rendered
    for service in ("backend", "worker", "ai-worker"):
        assert service in rendered
    assert "Ollama reachable from this container" in rendered


def test_failing_component_is_attributed_to_the_service(verify_module):
    reports = [report("backend", True), report("worker", False), report("ai-worker", True)]

    rendered = verify_module.render(reports, HOST, [])

    assert "Not ready:" in rendered
    assert "worker: ollama_reachable" in rendered
    # The host loopback hint is offered, because that is the usual Windows cause.
    assert "OLLAMA_HOST=0.0.0.0:11434" in rendered


def test_optional_components_do_not_fail_a_service(verify_module):
    reports = [report("backend", True)]
    reports[0]["components"][2]["ok"] = False  # offline_mode is informational

    rendered = verify_module.render(reports, HOST, [])

    assert "Not ready:" not in rendered


def test_skipped_service_is_reported_and_not_counted_ready(verify_module):
    rendered = verify_module.render([report("backend", True)], HOST, ["worker", "ai-worker"])

    assert "worker is not running" in rendered


def test_main_refuses_to_claim_readiness_without_docker(verify_module, monkeypatch, capsys):
    monkeypatch.setattr(verify_module, "docker_available", lambda: False)

    exit_code = verify_module.main(["--json"])
    payload = capsys.readouterr().out

    assert exit_code == 2
    assert "Docker Compose is not available" in payload


def test_main_fails_when_a_running_service_cannot_reach_ollama(verify_module, monkeypatch, capsys):
    monkeypatch.setattr(verify_module, "docker_available", lambda: True)
    monkeypatch.setattr(verify_module, "running_services", lambda: {"backend", "worker"})
    monkeypatch.setattr(
        verify_module,
        "probe_service",
        lambda service: report(service, service == "backend"),
    )
    monkeypatch.setattr(verify_module, "host_ollama_report", lambda: HOST)

    exit_code = verify_module.main(["--services", "backend", "worker"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "worker: ollama_reachable" in output


def test_main_passes_when_every_running_service_is_ready(verify_module, monkeypatch):
    monkeypatch.setattr(verify_module, "docker_available", lambda: True)
    monkeypatch.setattr(verify_module, "running_services", lambda: {"backend", "worker", "ai-worker"})
    monkeypatch.setattr(verify_module, "probe_service", lambda service: report(service, True))
    monkeypatch.setattr(verify_module, "host_ollama_report", lambda: HOST)

    assert verify_module.main([]) == 0


def test_main_fails_when_a_service_is_not_running(verify_module, monkeypatch):
    monkeypatch.setattr(verify_module, "docker_available", lambda: True)
    monkeypatch.setattr(verify_module, "running_services", lambda: {"backend"})
    monkeypatch.setattr(verify_module, "probe_service", lambda service: report(service, True))
    monkeypatch.setattr(verify_module, "host_ollama_report", lambda: HOST)

    assert verify_module.main([]) == 1
