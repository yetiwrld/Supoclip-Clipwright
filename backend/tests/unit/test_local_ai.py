"""Local-first AI behaviour: providers, model availability, offline guards.

These tests never touch the network: httpx is intercepted where the code probes
Ollama, and Whisper is never imported (the checkpoint lookup is filesystem-only).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src import config as config_module
from src import local_ai
from src.config import Config


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "LLM",
        "TRANSCRIPTION_PROVIDER",
        "WHISPER_MODEL",
        "WHISPER_MODEL_SIZE",
        "WHISPER_MODEL_DIR",
        "MODELS_DIR",
        "OFFLINE_MODE",
        "ALLOW_HOSTED_AI",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_are_local_and_offline(clean_env):
    config = Config()

    assert config.transcription_provider == "whisper"
    assert config.whisper_model == "small"
    assert config.llm == "ollama:llama3.2:3b"
    assert config.offline_mode is True
    assert config.allow_hosted_ai is False
    assert config.hosted_ai_enabled is False


def test_hosted_keys_do_not_change_the_default_model(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_env.setenv("GOOGLE_API_KEY", "g-test")

    config = Config()

    # A stray cloud key must not silently move analysis to a paid provider.
    assert config.llm == "ollama:llama3.2:3b"
    assert config.hosted_ai_enabled is False


def test_hosted_default_is_opt_in(clean_env):
    clean_env.setenv("OFFLINE_MODE", "false")
    clean_env.setenv("ALLOW_HOSTED_AI", "true")
    clean_env.setenv("OPENAI_API_KEY", "sk-test")

    config = Config()

    assert config.llm == "openai:gpt-5.2"
    assert config.hosted_ai_enabled is True


def test_offline_mode_wins_over_allow_hosted_ai(clean_env):
    clean_env.setenv("ALLOW_HOSTED_AI", "true")
    clean_env.setenv("OFFLINE_MODE", "true")

    assert Config().hosted_ai_enabled is False


def test_unknown_transcription_provider_does_not_fall_back_to_assemblyai(clean_env):
    clean_env.setenv("TRANSCRIPTION_PROVIDER", "assemblyai-typo")

    assert Config().transcription_provider == "whisper"


def test_whisper_model_aliases_and_env_override(clean_env):
    clean_env.setenv("WHISPER_MODEL_SIZE", "base")
    assert Config().whisper_model == "base"

    clean_env.setenv("WHISPER_MODEL_SIZE", "large")
    assert Config().whisper_model == "large-v3"

    clean_env.setenv("WHISPER_MODEL", "tiny")
    assert Config().whisper_model == "tiny"


def test_model_directory_defaults_and_overrides(clean_env, tmp_path):
    default_config = Config()
    assert Path(default_config.whisper_model_dir) == Path(default_config.models_dir) / "whisper"

    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path / "whisper"))
    assert Path(Config().whisper_model_dir) == tmp_path / "whisper"


# ---------------------------------------------------------------------------
# Whisper checkpoint discovery
# ---------------------------------------------------------------------------


def test_whisper_checkpoint_is_found_in_the_persistent_directory(clean_env, tmp_path):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    clean_env.setenv("WHISPER_MODEL_SIZE", "base")
    (tmp_path / "base.pt").write_bytes(b"x" * 1024)

    status = local_ai.whisper_model_status(Config())

    assert status["available"] is True
    assert status["model"] == "base"
    assert Path(status["path"]) == tmp_path / "base.pt"


def test_missing_whisper_model_reports_an_actionable_message(clean_env, tmp_path):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path / "missing"))
    clean_env.setenv("WHISPER_MODEL_SIZE", "small")

    status = local_ai.whisper_model_status(Config())

    assert status["available"] is False
    assert "setup_local_ai.py" in status["message"]
    assert "small" in status["message"]
    with pytest.raises(local_ai.LocalModelUnavailable):
        local_ai.ensure_whisper_model(Config())


def test_switching_model_size_changes_the_expected_checkpoint(clean_env, tmp_path):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    (tmp_path / "base.pt").write_bytes(b"x")
    (tmp_path / "small.pt").write_bytes(b"y")

    clean_env.setenv("WHISPER_MODEL_SIZE", "base")
    base = local_ai.whisper_model_status(Config())
    clean_env.setenv("WHISPER_MODEL_SIZE", "small")
    small = local_ai.whisper_model_status(Config())

    assert base["path"] != small["path"]
    assert Path(base["path"]).name == "base.pt"
    assert Path(small["path"]).name == "small.pt"


# ---------------------------------------------------------------------------
# Ollama probing
# ---------------------------------------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    """Intercept the Ollama probe and record the URLs that were called."""

    calls: list[str] = []
    state = {"payload": {"models": [{"name": "llama3.2:3b"}]}, "status": 200}

    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(state["status"], json=state["payload"])

    monkeypatch.setattr(
        local_ai.httpx,
        "get",
        lambda url, **kwargs: real_client(transport=httpx.MockTransport(handler)).get(url, **kwargs),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: real_client(transport=httpx.MockTransport(handler)).get(url, **kwargs),
    )
    state["calls"] = calls
    return state


def _config(**overrides):
    base = dict(
        llm="ollama:llama3.2:3b",
        offline_mode=True,
        allow_hosted_ai=False,
        transcription_provider="whisper",
        whisper_model="small",
        whisper_model_dir="/tmp/does-not-matter",
        models_dir="/tmp/models",
        resolve_ollama_base_url=lambda: "http://localhost:11434/v1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_native_base_url_drops_the_openai_v1_suffix():
    assert local_ai.native_ollama_base_url(_config()) == "http://localhost:11434"
    docker = _config(resolve_ollama_base_url=lambda: "http://host.docker.internal:11434/v1/")
    assert local_ai.native_ollama_base_url(docker) == "http://host.docker.internal:11434"


def test_ollama_status_reports_installed_model(ollama):
    status = local_ai.llm_status(_config())

    assert status["local"] is True
    assert status["reachable"] is True
    assert status["model_available"] is True
    assert status["installed_models"] == ["llama3.2:3b"]
    assert ollama["calls"] == ["http://localhost:11434/api/tags"]


def test_ollama_running_but_model_missing_is_an_error_not_a_fallback(ollama):
    ollama["payload"] = {"models": [{"name": "qwen2.5:7b"}]}

    status = local_ai.llm_status(_config())

    assert status["reachable"] is True
    assert status["model_available"] is False
    assert "ollama pull llama3.2:3b" in status["message"]
    with pytest.raises(local_ai.LocalModelUnavailable):
        local_ai.ensure_ollama_ready(_config())


def test_ollama_down_reports_the_daemon(monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: httpx.Client(transport=httpx.MockTransport(refuse)).get(url, **kwargs),
    )

    status = local_ai.llm_status(_config())

    assert status["reachable"] is False
    assert status["model_available"] is False
    assert "not reachable" in status["message"]
    with pytest.raises(local_ai.LocalModelUnavailable):
        local_ai.ensure_ollama_ready(_config())


def test_hosted_provider_is_refused_offline(ollama):
    status = local_ai.llm_status(_config(llm="openai:gpt-5.2"))

    assert status["local"] is False
    assert status["model_available"] is False
    assert "offline mode" in status["message"].lower()
    assert ollama["calls"] == []  # nothing was called


def test_model_name_matching_handles_the_latest_tag():
    installed = ["llama3.2:latest", "qwen2.5:7b"]

    assert local_ai.model_is_installed(installed, "llama3.2")
    assert local_ai.model_is_installed(installed, "llama3.2:latest")
    # A tag mismatch is a different model: 3b must not satisfy an untagged pull.
    assert not local_ai.model_is_installed(["llama3.2:3b"], "llama3.2")
    assert not local_ai.model_is_installed(installed, "llama3.2:1b")


# ---------------------------------------------------------------------------
# Status payload + worker preflight
# ---------------------------------------------------------------------------


def test_status_payload_is_complete_and_marks_online_features(clean_env, tmp_path, ollama, monkeypatch):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    (tmp_path / "small.pt").write_bytes(b"x")
    monkeypatch.setattr(local_ai, "check_ffmpeg", lambda: {
        "available": True, "path": "/usr/bin/ffmpeg", "ffprobe_available": True, "message": None,
    })

    status = local_ai.local_ai_status(Config())

    assert status["app"] == "ClipWright Studio"
    assert status["offline_mode"] is True
    assert status["ready_for_offline_processing"] is True
    assert status["transcription"]["whisper"]["available"] is True
    assert status["llm"]["model_available"] is True
    ids = [check["id"] for check in status["checks"]]
    assert ids == ["whisper_model", "ollama", "llm_model", "ffmpeg"]
    feature_ids = {feature["id"] for feature in status["optional_online_features"]}
    assert {"stream_downloads", "broll", "social_publishing", "hosted_ai"} <= feature_ids
    assert all(f["requires_internet"] for f in status["optional_online_features"])


def test_status_is_not_ready_when_whisper_is_missing(clean_env, tmp_path, ollama, monkeypatch):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(local_ai, "check_ffmpeg", lambda: {
        "available": True, "path": "/usr/bin/ffmpeg", "ffprobe_available": True, "message": None,
    })

    status = local_ai.local_ai_status(Config())

    assert status["ready_for_offline_processing"] is False
    assert status["checks"][0]["ok"] is False


def test_preflight_collects_every_problem(clean_env, tmp_path, ollama, monkeypatch):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(local_ai, "check_ffmpeg", lambda: {
        "available": False, "path": "", "ffprobe_available": False, "message": "FFmpeg was not found on PATH.",
    })
    ollama["payload"] = {"models": []}

    problems = local_ai.preflight_task(Config())

    joined = " ".join(problems)
    assert "Whisper" in joined
    assert "ollama pull llama3.2:3b" in joined
    assert "FFmpeg" in joined


def test_preflight_is_clean_when_everything_is_installed(clean_env, tmp_path, ollama, monkeypatch):
    clean_env.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    (tmp_path / "small.pt").write_bytes(b"x")
    monkeypatch.setattr(local_ai, "check_ffmpeg", lambda: {
        "available": True, "path": "/usr/bin/ffmpeg", "ffprobe_available": True, "message": None,
    })

    assert local_ai.preflight_task(Config()) == []


def test_preflight_rejects_hosted_transcription_offline(clean_env, tmp_path, ollama, monkeypatch):
    clean_env.setenv("TRANSCRIPTION_PROVIDER", "assemblyai")
    clean_env.setenv("ASSEMBLY_AI_API_KEY", "key")
    monkeypatch.setattr(local_ai, "check_ffmpeg", lambda: {
        "available": True, "path": "/usr/bin/ffmpeg", "ffprobe_available": True, "message": None,
    })

    problems = local_ai.preflight_task(Config())

    assert any("assemblyai" in problem and "offline" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Setup script model catalogue
# ---------------------------------------------------------------------------


def test_setup_script_and_runtime_share_one_model_catalogue():
    from src import whisper_models

    script = (Path(__file__).resolve().parents[3] / "scripts" / "setup_local_ai.py").read_text()
    assert "whisper_models" in script
    assert whisper_models.checkpoint_filename("small") == "small.pt"
    assert whisper_models.expected_sha256("small") == "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"
    assert whisper_models.normalize_model_name("turbo") == "large-v3-turbo"
