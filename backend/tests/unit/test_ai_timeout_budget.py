"""Configurable local-analysis timeouts and the layers that must not undercut them.

The shipped default used to be a hard-coded 600 s per window, which surfaced on
ordinary CPU-only hardware as "The AI analysis service took too long to
respond". These tests pin the new contract:

* ``AI_ANALYSIS_TIMEOUT_SECONDS`` is the per-window budget, default 1800 s,
  validated and clamped;
* the whole-run budget derives from it and is never shorter than one window;
* the Ollama HTTP read timeout and the ARQ job timeout follow it;
* slow responses and invalid model output are logged as different failures.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from src import ai
from src.config import (
    DEFAULT_AI_ANALYSIS_TIMEOUT_SECONDS,
    MAX_AI_ANALYSIS_TIMEOUT_SECONDS,
    MIN_AI_ANALYSIS_TIMEOUT_SECONDS,
    Config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from "no explicit budget" and no agent cache."""
    for name in (
        "AI_ANALYSIS_TIMEOUT_SECONDS",
        "AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS",
        "PROCESSING_TASK_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai, "_transcript_agent", None)
    monkeypatch.setattr(ai, "_transcript_agent_signature", None)
    monkeypatch.setattr(ai, "_rerank_agent", None)
    monkeypatch.setattr(ai, "_rerank_agent_signature", None)
    yield


# ---------------------------------------------------------------------------
# Defaults, validation and clamping
# ---------------------------------------------------------------------------


def test_default_budget_is_thirty_minutes_per_window():
    assert DEFAULT_AI_ANALYSIS_TIMEOUT_SECONDS == 1800
    assert ai.TRANSCRIPT_ANALYSIS_TIMEOUT_SECONDS == 1800
    assert Config().ai_analysis_timeout_seconds == 1800
    assert ai.resolve_analysis_timeout_seconds() == 1800


def test_explicit_budget_from_the_environment_is_used(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "2700")

    assert Config().ai_analysis_timeout_seconds == 2700
    assert ai.resolve_analysis_timeout_seconds() == 2700


def test_blank_budget_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "   ")

    assert Config().ai_analysis_timeout_seconds == 1800


@pytest.mark.parametrize("raw", ["abc", "18oo", "1800s", "1.5"])
def test_non_numeric_budget_falls_back_to_the_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", raw)

    with caplog.at_level("WARNING", logger="src.config"):
        value = Config().ai_analysis_timeout_seconds

    assert value == 1800
    assert "AI_ANALYSIS_TIMEOUT_SECONDS" in caplog.text


def test_tiny_budget_is_clamped_up_to_the_minimum(monkeypatch, caplog):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "1")

    with caplog.at_level("WARNING", logger="src.config"):
        value = Config().ai_analysis_timeout_seconds

    assert value == MIN_AI_ANALYSIS_TIMEOUT_SECONDS == 60
    assert "minimum" in caplog.text


def test_absurd_budget_is_clamped_down_to_the_maximum(monkeypatch, caplog):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "999999999")

    with caplog.at_level("WARNING", logger="src.config"):
        value = Config().ai_analysis_timeout_seconds

    assert value == MAX_AI_ANALYSIS_TIMEOUT_SECONDS
    assert "maximum" in caplog.text


# ---------------------------------------------------------------------------
# Whole-run budget never undercuts a single window
# ---------------------------------------------------------------------------


def test_total_budget_covers_every_batch_of_windows():
    # 9 windows through 3 concurrent slots = 3 batches of 1800 s.
    assert ai.resolve_analysis_total_timeout_seconds(9, 1800) == 5400


def test_total_budget_is_never_shorter_than_one_window(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS", "60")

    total = ai.resolve_analysis_total_timeout_seconds(9)

    assert total >= 1800
    assert total == 5400


def test_total_budget_can_be_extended_by_the_operator(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS", "7200")

    assert ai.resolve_analysis_total_timeout_seconds(3, 1800) == 7200


def test_total_budget_is_capped(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS", "999999999")

    assert ai.resolve_analysis_total_timeout_seconds(3, 1800) == 12 * 3600


# ---------------------------------------------------------------------------
# The per-window budget is what actually bounds a run
# ---------------------------------------------------------------------------


async def test_env_budget_overrides_the_module_default(monkeypatch):
    """A slow-but-working model must not be killed by a stale hard-coded value."""
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "60")
    monkeypatch.setattr(ai, "TRANSCRIPT_ANALYSIS_TIMEOUT_SECONDS", 0.01)

    async def slow_but_fine(_prompt):
        await ai.asyncio.sleep(0.05)
        return SimpleNamespace(output="ok")

    agent = SimpleNamespace(run=AsyncMock(side_effect=slow_but_fine))

    assert (await ai._run_transcript_analysis(agent, "prompt")).output == "ok"


async def test_module_default_bounds_a_run_when_env_is_absent(monkeypatch):
    monkeypatch.setattr(ai, "TRANSCRIPT_ANALYSIS_TIMEOUT_SECONDS", 0.01)

    async def never_finishes(_prompt):
        await ai.asyncio.Event().wait()

    agent = SimpleNamespace(run=AsyncMock(side_effect=never_finishes))

    with pytest.raises(RuntimeError, match="took too long"):
        await ai._run_transcript_analysis(agent, "prompt")


async def test_slow_model_is_logged_as_a_timeout_not_invalid_output(monkeypatch, caplog):
    monkeypatch.setattr(ai, "TRANSCRIPT_ANALYSIS_TIMEOUT_SECONDS", 0.02)

    async def never_finishes(_prompt):
        await ai.asyncio.Event().wait()

    agent = SimpleNamespace(run=AsyncMock(side_effect=never_finishes))

    with caplog.at_level("ERROR", logger="src.ai"):
        with pytest.raises(RuntimeError, match="took too long"):
            await ai._run_transcript_analysis(agent, "prompt")

    assert "time budget" in caplog.text
    assert "invalid structured output" not in caplog.text


async def test_invalid_structured_output_is_reported_separately(caplog):
    agent = SimpleNamespace(
        run=AsyncMock(
            side_effect=UnexpectedModelBehavior("Invalid JSON: expected a ranking list")
        )
    )

    with caplog.at_level("ERROR", logger="src.ai"):
        with pytest.raises(UnexpectedModelBehavior):
            await ai._run_transcript_analysis(agent, "prompt")

    assert "invalid structured output" in caplog.text
    assert "time budget" not in caplog.text
    # Bounded: a schema failure is not retried over and over.
    assert agent.run.await_count == 1


async def test_window_analysis_derives_its_budget_from_the_window_count(monkeypatch):
    seen: dict = {}

    def fake_total(window_count, per_window_seconds=None, runtime_config=None):
        seen["windows"] = window_count
        seen["per_window"] = per_window_seconds
        return 30.0

    async def fake_run(_agent, _prompt, timeout_seconds=None):
        assert timeout_seconds is None, "windows resolve the configured budget themselves"
        return SimpleNamespace(output="analysis")

    monkeypatch.setattr(ai, "_run_transcript_analysis", fake_run)
    monkeypatch.setattr(ai, "resolve_analysis_total_timeout_seconds", fake_total)
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "2400")

    windows = [
        {"start": 0, "end": 600, "transcript": "[00:00 - 00:10] hello"},
        {"start": 540, "end": 1200, "transcript": "[09:00 - 20:00] more"},
        {"start": 1140, "end": 1800, "transcript": "[19:00 - 30:00] last"},
        {"start": 1740, "end": 2100, "transcript": "[29:00 - 35:00] tail"},
    ]
    analyses = await ai._analyze_transcript_windows(SimpleNamespace(), windows, None)

    assert seen["windows"] == 4
    assert seen["per_window"] == 2400
    assert len(analyses) == 4


# ---------------------------------------------------------------------------
# Ollama HTTP client follows the same budget
# ---------------------------------------------------------------------------


async def test_ollama_client_read_timeout_matches_the_budget(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "2400")

    client = ai.build_ollama_http_client(ai.resolve_analysis_timeout_seconds())

    try:
        assert client.timeout.read == 2400
        # A stopped Ollama must fail quickly, not hang for the whole budget.
        assert client.timeout.connect == 10
    finally:
        await client.aclose()


def test_ollama_provider_receives_the_client(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "2400")
    captured: dict = {}

    class RecorderProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class RecorderModel:
        def __init__(self, name, **kwargs):
            captured["model"] = name
            captured["model_kwargs"] = kwargs

    monkeypatch.setattr(ai, "OllamaProvider", RecorderProvider)
    monkeypatch.setattr(ai, "OllamaModel", RecorderModel)

    ai._build_transcript_model(Config(), "ollama:llama3.2:3b")

    http_client = captured["http_client"]
    assert http_client.timeout.read == 2400
    assert captured["model"] == "llama3.2:3b"


# ---------------------------------------------------------------------------
# Worker job timeout is derived, never shorter than the analysis budget
# ---------------------------------------------------------------------------


def test_worker_job_timeout_keeps_the_three_hour_floor():
    assert Config().worker_job_timeout_seconds >= 10800


def test_worker_job_timeout_grows_with_the_analysis_budget(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "3600")

    config = Config()

    assert config.worker_job_timeout_seconds > 10800
    assert config.worker_job_timeout_seconds >= 3600
    # The stale-processing sweep must stay above the worker's own deadline.
    assert config.processing_task_timeout_seconds > config.worker_job_timeout_seconds


def test_worker_settings_use_the_derived_timeout():
    arq_worker = pytest.importorskip("arq.worker")
    assert arq_worker is not None

    from src.config import Config as RuntimeConfig
    from src.workers import tasks

    assert tasks.WorkerSettings.job_timeout == RuntimeConfig().worker_job_timeout_seconds
    assert tasks.WorkerSettings.job_timeout >= 10800
    assert tasks.WorkerSettings.max_tries == 3


async def test_rerank_uses_the_configured_budget_instead_of_a_shorter_cap(monkeypatch):
    """A raised budget must also cover the comparative rerank pass."""
    seen: list[float | None] = []

    async def fake_run(_agent, _prompt, timeout_seconds=None):
        seen.append(timeout_seconds)
        return SimpleNamespace(output=SimpleNamespace(ranking=[]))

    monkeypatch.setattr(ai, "_run_transcript_analysis", fake_run)
    monkeypatch.setattr(ai, "get_rerank_agent", lambda: SimpleNamespace())
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "2400")

    candidates = [
        ai.TranscriptSegment(start_time="00:00", end_time="00:30", text="first idea"),
        ai.TranscriptSegment(start_time="01:00", end_time="01:30", text="second idea"),
    ]
    await ai.rerank_candidates(candidates)

    assert seen == [2400]


def test_worker_timeout_reserves_a_slot_for_the_rerank(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_TIMEOUT_SECONDS", "1800")

    config = Config()
    per_window = config.ai_analysis_timeout_seconds
    windows = config._derive_analysis_windows()
    batches = -(-windows // 3)  # ceil division, 3 concurrent windows

    assert config.worker_job_timeout_seconds >= per_window * (batches + 1)


# ---------------------------------------------------------------------------
# Every container that analyses or renders must receive the same budget
# ---------------------------------------------------------------------------


def test_compose_passes_the_budget_to_backend_and_both_workers():
    yaml = pytest.importorskip("yaml")
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((repo_root / "docker-compose.yml").read_text())
    services = compose["services"]

    for service in ("backend", "worker", "ai-worker"):
        env = services[service]["environment"]
        assert any(
            entry.startswith("AI_ANALYSIS_TIMEOUT_SECONDS=") for entry in env
        ), f"{service} would fall back to a shorter internal default"
        assert any(
            entry.startswith("AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS=") for entry in env
        ), f"{service} cannot receive AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS"
