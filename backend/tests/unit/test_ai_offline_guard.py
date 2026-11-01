"""Offline guards for transcript analysis: local by default, no silent cloud.

Every test here is a regression test for "the app must never call a hosted AI
provider unless the operator explicitly turned that on".
"""

from types import SimpleNamespace

import pytest
from pydantic_ai.models.ollama import OllamaModel

from src import ai


def _config(**overrides):
    base = dict(
        llm="ollama:llama3.2:3b",
        offline_mode=True,
        allow_hosted_ai=False,
        hosted_ai_enabled=False,
        openai_api_key="sk-should-be-ignored",
        google_api_key="g-should-be-ignored",
        anthropic_api_key="a-should-be-ignored",
        openrouter_api_key="o-should-be-ignored",
        openrouter_fallback_model="openai/gpt-6-luna",
        ollama_api_key=None,
        ollama_base_url=None,
        resolve_ollama_base_url=lambda: "http://localhost:11434/v1",
        as_runtime_settings=lambda: {},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "model",
    [
        "openai:gpt-5.2",
        "google-gla:gemini-3-flash-preview",
        "anthropic:claude-4-sonnet",
        "openrouter:anthropic/claude-sonnet-5.5",
    ],
)
def test_hosted_models_are_refused_while_offline(model):
    error = ai._get_missing_llm_key_error(model, _config())

    assert error is not None
    assert "offline" in error.lower()
    assert "ollama:llama3.2:3b" in error


def test_hosted_models_are_allowed_when_explicitly_enabled():
    config = _config(offline_mode=False, allow_hosted_ai=True, hosted_ai_enabled=True)

    assert ai._get_missing_llm_key_error("openai:gpt-5.2", config) is None


def test_unknown_provider_is_rejected():
    error = ai._get_missing_llm_key_error("magic:model", _config())

    assert error is not None
    assert "Unsupported LLM provider" in error


def test_ollama_needs_no_api_key():
    assert ai._get_missing_llm_key_error("ollama:llama3.2:3b", _config()) is None


def test_default_model_builds_a_local_ollama_model():
    model = ai._build_transcript_model(_config())

    assert isinstance(model, OllamaModel)
    assert model.model_name == "llama3.2:3b"
    assert "localhost:11434" in str(model.base_url)


def test_agent_construction_fails_loudly_when_ollama_is_down(monkeypatch):
    """A stopped Ollama must raise, never fall back to a hosted provider."""
    monkeypatch.setattr(ai, "get_config", lambda: _config())
    monkeypatch.setattr(ai, "apply_settings_to_process_env", lambda settings: None)
    ai._transcript_agent = None
    ai._transcript_agent_signature = None

    from src import local_ai

    def unavailable(config=None):
        raise local_ai.LocalModelUnavailable(
            "Ollama is not reachable at http://localhost:11434. "
            "Install Ollama (https://ollama.com/download), start it, then run: ollama pull llama3.2:3b"
        )

    monkeypatch.setattr(local_ai, "ensure_ollama_ready", unavailable)

    with pytest.raises(RuntimeError, match="Ollama is not reachable"):
        ai.get_transcript_agent()
    ai._transcript_agent = None
    ai._transcript_agent_signature = None


def test_agent_construction_rejects_hosted_provider_offline(monkeypatch):
    monkeypatch.setattr(ai, "get_config", lambda: _config(llm="openai:gpt-5.2"))
    monkeypatch.setattr(ai, "apply_settings_to_process_env", lambda settings: None)
    ai._transcript_agent = None
    ai._transcript_agent_signature = None

    with pytest.raises(RuntimeError, match="offline"):
        ai.get_transcript_agent()
    ai._transcript_agent = None
    ai._transcript_agent_signature = None


def test_openrouter_cloud_failover_is_only_reachable_when_opted_in():
    """The only fallback that exists is inside an opted-in hosted provider."""
    config = _config(
        llm="openrouter:anthropic/claude-sonnet-5.5",
        offline_mode=False,
        allow_hosted_ai=True,
        hosted_ai_enabled=True,
    )

    model = ai._build_transcript_model(config)

    # The fallback lives in OpenRouter's own routing, which is unreachable in
    # offline mode because the agent refuses to build at all (asserted above).
    assert model.model_name == "anthropic/claude-sonnet-5.5"
