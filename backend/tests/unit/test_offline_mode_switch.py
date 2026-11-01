"""The runtime Offline Mode switch.

``.env`` is the deploy default; Settings can persist an override that the API
and both workers read. These tests cover the three properties that matter:

* the switch is really persisted (survives a new ``Config``),
* it cannot enable hosted AI on its own,
* a missing, corrupt or read-only file never breaks a job.
"""

from __future__ import annotations

import json

import pytest

from src import offline_mode
from src.config import Config


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    monkeypatch.setenv("CLIPWRIGHT_CONFIG_DIR", str(directory))
    offline_mode.reset_cache()
    yield directory
    offline_mode.reset_cache()


def test_env_value_is_used_when_no_override_exists(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    config = Config()

    assert config.offline_mode is True
    assert config.offline_mode_source == "env"
    assert config.offline_mode_from_env is True


def test_runtime_switch_is_persisted_and_read_by_a_new_config(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_HOSTED_AI", "false")

    assert offline_mode.write_override(False) is False

    stored = json.loads((config_dir / offline_mode.FILE_NAME).read_text())
    assert stored == {"offline_mode": False}

    config = Config()
    assert config.offline_mode is False
    assert config.offline_mode_source == "runtime"
    # ...but the .env default is still reported, so the UI can offer a reset.
    assert config.offline_mode_from_env is True


def test_runtime_switch_cannot_enable_hosted_ai_by_itself(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_HOSTED_AI", "false")
    offline_mode.write_override(False)

    config = Config()

    assert config.offline_mode is False
    assert config.hosted_ai_enabled is False


def test_hosted_ai_requires_both_switches(config_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ALLOW_HOSTED_AI", "true")
    monkeypatch.setenv("OFFLINE_MODE", "false")
    assert Config().hosted_ai_enabled is True

    offline_mode.write_override(True)
    config = Config()

    assert config.offline_mode is True
    assert config.hosted_ai_enabled is False
    # The LLM falls back to the local model instead of a hosted one.
    assert config.llm.startswith("ollama:")


def test_clearing_the_override_returns_to_the_env_default(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    offline_mode.write_override(False)
    assert Config().offline_mode is False

    offline_mode.write_override(None)

    assert not (config_dir / offline_mode.FILE_NAME).exists()
    config = Config()
    assert config.offline_mode is True
    assert config.offline_mode_source == "env"


def test_corrupt_state_file_is_ignored(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    config_dir.mkdir(parents=True)
    (config_dir / offline_mode.FILE_NAME).write_text("{not json")

    config = Config()

    assert config.offline_mode is True
    assert config.offline_mode_source == "env"


def test_state_file_without_boolean_is_ignored(config_dir, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    config_dir.mkdir(parents=True)
    (config_dir / offline_mode.FILE_NAME).write_text(json.dumps({"offline_mode": "yes"}))

    assert Config().offline_mode is True


def test_unwritable_state_directory_raises_oserror(config_dir, monkeypatch):
    # Point the state path at a file, so mkdir() cannot succeed.
    blocker = config_dir.parent / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("CLIPWRIGHT_CONFIG_DIR", str(blocker))
    offline_mode.reset_cache()

    with pytest.raises(OSError):
        offline_mode.write_override(True)


def test_state_path_defaults_to_the_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CLIPWRIGHT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLIPWRIGHT_DATA_DIR", str(tmp_path / "data"))

    assert offline_mode.state_path() == tmp_path / "data" / "config" / offline_mode.FILE_NAME


def test_status_payload_reports_the_switch(config_dir, monkeypatch):
    from src.local_ai import local_ai_status
    from src import local_ai

    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_HOSTED_AI", "false")
    offline_mode.write_override(False)

    # Keep the probe offline and deterministic.
    monkeypatch.setattr(
        local_ai,
        "check_ollama",
        lambda config=None, timeout=None: {
            "base_url": "http://localhost:11434",
            "openai_base_url": "http://localhost:11434/v1",
            "reachable": True,
            "models": ["llama3.2:3b"],
            "error": None,
        },
    )

    status = local_ai_status(Config())

    assert status["offline_mode"] is False
    assert status["offline_mode_source"] == "runtime"
    assert status["offline_mode_from_env"] is True
    assert status["hosted_ai_available"] is False
    assert status["local_only"] is True
    assert status["offline_mode_state_path"].endswith(offline_mode.FILE_NAME)
