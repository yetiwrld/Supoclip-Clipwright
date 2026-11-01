"""The in-container probe CLI and the Docker→host Ollama contract.

`python -m src.local_ai --check` is what `scripts/verify_local_ai.py` executes
inside backend, worker and ai-worker. These tests pin the properties that make
it trustworthy:

* it fails (non-zero) and names the component when something is missing,
* it passes only when Whisper is readable, the model is installed, FFmpeg exists
  and the storage mounts are writable from *this* process,
* the Ollama base URL follows the container boundary, which is the difference
  between working and "connection refused" on Windows.
"""

from __future__ import annotations

import json

import pytest

from src import local_ai
from src.config import Config, DOCKER_OLLAMA_BASE_URL, LOCAL_OLLAMA_BASE_URL


@pytest.fixture()
def fake_ollama(monkeypatch):
    def _install(models=("llama3.2:3b",), reachable=True):
        monkeypatch.setattr(
            local_ai,
            "check_ollama",
            lambda config=None, timeout=None: {
                "base_url": "http://host.docker.internal:11434",
                "openai_base_url": "http://host.docker.internal:11434/v1",
                "reachable": reachable,
                "models": list(models) if reachable else [],
                "error": None if reachable else "ConnectError",
            },
        )

    _install()
    return _install


@pytest.fixture()
def ready_env(tmp_path, monkeypatch, fake_ollama):
    """A filesystem that looks like a correctly mounted container."""
    whisper_dir = tmp_path / "models" / "whisper"
    whisper_dir.mkdir(parents=True)
    (whisper_dir / "small.pt").write_bytes(b"0" * 4096)
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "small")
    monkeypatch.setenv("WHISPER_MODEL_DIR", str(whisper_dir))
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("TEMP_DIR", str(uploads))
    monkeypatch.setenv("LLM", "ollama:llama3.2:3b")
    # FFmpeg is part of every image; a sandbox without it should not decide.
    monkeypatch.setattr(
        local_ai,
        "check_ffmpeg",
        lambda: {
            "available": True,
            "path": "/usr/bin/ffmpeg",
            "ffprobe_available": True,
            "message": None,
        },
    )
    return tmp_path


def test_reports_ready_when_every_component_is_usable(ready_env, capsys):
    exit_code = local_ai.main(["--check"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "RESULT: READY" in output
    assert "Ollama reachable from this container" in output


def test_reports_ready_as_json(ready_env, capsys):
    exit_code = local_ai.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["failing_components"] == []
    assert payload["llm"] == "ollama:llama3.2:3b"


def test_fails_and_names_the_component_when_ollama_is_down(ready_env, capsys, fake_ollama):
    fake_ollama(reachable=False)

    exit_code = local_ai.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert payload["ready"] is False
    assert "ollama_reachable" in payload["failing_components"]
    assert "ollama_model" in payload["failing_components"]


def test_fails_when_the_model_is_missing_from_ollama(ready_env, capsys, fake_ollama):
    fake_ollama(models=("qwen2.5:7b",))

    exit_code = local_ai.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert payload["failing_components"] == ["ollama_model"]
    detail = next(c for c in payload["components"] if c["id"] == "ollama_model")["detail"]
    assert "ollama pull llama3.2:3b" in detail


def test_fails_when_the_whisper_checkpoint_is_unreadable(ready_env, capsys):
    checkpoint = ready_env / "models" / "whisper" / "small.pt"
    checkpoint.write_bytes(b"")
    checkpoint.chmod(0o000)
    try:
        exit_code = local_ai.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert exit_code != 0
        assert "whisper_readable" in payload["failing_components"]
    finally:
        checkpoint.chmod(0o600)


def test_fails_when_a_mount_cannot_be_created(ready_env, capsys, monkeypatch):
    # A path *under a regular file* can never be created, which is what a broken
    # bind mount looks like from inside a container.
    blocker = ready_env / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("TEMP_DIR", str(blocker / "uploads"))

    exit_code = local_ai.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert "storage_uploads" in payload["failing_components"]
    detail = next(c for c in payload["components"] if c["id"] == "storage_uploads")["detail"]
    assert "CLIPWRIGHT_DATA_DIR" in detail
    assert "cannot be created" in detail


def test_offline_mode_is_reported_but_never_a_failure(ready_env, capsys, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    exit_code = local_ai.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    offline_row = next(c for c in payload["components"] if c["id"] == "offline_mode")
    assert offline_row["ok"] is True
    assert offline_row["required"] is False
    assert exit_code == 0


def test_exit_code_counts_only_required_failures():
    components = [
        {"id": "a", "ok": False, "required": True},
        {"id": "b", "ok": False, "required": False},
        {"id": "c", "ok": True, "required": True},
    ]
    assert local_ai.verify_exit_code(components) == 1


# ---------------------------------------------------------------------------
# Docker networking: the containers must talk to the Windows host, not itself.
# ---------------------------------------------------------------------------


def test_ollama_url_uses_the_host_gateway_inside_docker(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(local_ai.os.path, "exists", lambda path: str(path) == "/.dockerenv")

    config = Config()

    assert config.resolve_ollama_base_url() == DOCKER_OLLAMA_BASE_URL
    assert "host.docker.internal" in config.resolve_ollama_base_url()


def test_ollama_url_uses_loopback_outside_docker(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(local_ai.os.path, "exists", lambda path: False)

    assert Config().resolve_ollama_base_url() == LOCAL_OLLAMA_BASE_URL


def test_native_ollama_root_is_derived_from_the_compatible_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
    config = Config()

    assert local_ai.native_ollama_base_url(config) == "http://host.docker.internal:11434"


def test_docker_ollama_service_url_is_honoured(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
    config = Config()

    assert local_ai.native_ollama_base_url(config) == "http://ollama:11434"
