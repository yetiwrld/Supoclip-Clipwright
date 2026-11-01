"""Local Whisper transcription: model loading, offline safety, provider routing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import local_ai
from src.media import transcription


@pytest.fixture
def whisper_env(monkeypatch, tmp_path):
    """A checkpoint on disk plus a stubbed openai-whisper module."""
    loaded: list[dict] = []

    class FakeModel:
        def __init__(self, name):
            self.name = name

        def transcribe(self, audio_path, **kwargs):
            return {
                "text": " Hello world. ",
                "language": "en",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.5,
                        "text": "Hello world.",
                        "words": [
                            {"word": "Hello", "start": 0.0, "end": 0.7, "probability": 0.9},
                            {"word": "world.", "start": 0.7, "end": 1.5, "probability": 0.8},
                        ],
                    }
                ],
            }

    def load_model(name, device=None, download_root=None, in_memory=False):
        loaded.append({"name": name, "device": device, "download_root": download_root})
        return FakeModel(name)

    monkeypatch.setattr(transcription, "_WHISPER_AVAILABLE", True)
    monkeypatch.setattr(transcription, "_whisper", SimpleNamespace(load_model=load_model))
    transcription._WHISPER_MODEL_CACHE.clear()

    (tmp_path / "small.pt").write_bytes(b"checkpoint")
    (tmp_path / "base.pt").write_bytes(b"checkpoint")

    config = SimpleNamespace(
        whisper_model="small",
        whisper_model_dir=str(tmp_path),
        whisper_device="cpu",
        offline_mode=True,
        transcription_language="",
        transcription_provider="whisper",
        allow_hosted_ai=False,
        assembly_ai_api_key=None,
    )
    yield config, loaded
    transcription._WHISPER_MODEL_CACHE.clear()


def test_model_loads_from_the_persistent_directory(whisper_env):
    config, loaded = whisper_env

    model = transcription._get_whisper_model("small", config)

    assert model.name == "small"
    assert loaded == [
        {"name": "small", "device": "cpu", "download_root": str(Path(config.whisper_model_dir))}
    ]


def test_model_cache_is_keyed_by_size(whisper_env):
    config, loaded = whisper_env

    first = transcription._get_whisper_model("small", config)
    again = transcription._get_whisper_model("small", config)
    other = transcription._get_whisper_model("base", config)

    assert first is again
    assert other is not first
    assert [entry["name"] for entry in loaded] == ["small", "base"]


def test_missing_checkpoint_never_reaches_the_downloader(whisper_env, tmp_path):
    config, loaded = whisper_env
    Path(config.whisper_model_dir, "small.pt").unlink()

    with pytest.raises(local_ai.LocalModelUnavailable) as error:
        transcription._get_whisper_model("small", config)

    assert "setup_local_ai.py" in str(error.value)
    # load_model() would have downloaded the weights from the network.
    assert loaded == []


def test_transcribe_uses_word_timestamps_and_iso_language(whisper_env, monkeypatch, tmp_path):
    config, _ = whisper_env
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(transcription, "_prepare_audio_for_transcription", lambda path: audio)

    calls = {}

    class FakeModel:
        def transcribe(self, path, **kwargs):
            calls["path"] = path
            calls.update(kwargs)
            return {"text": "Hello world.", "segments": [], "language": "en"}

    monkeypatch.setattr(transcription, "_get_whisper_model", lambda name, cfg: FakeModel())

    result = transcription.transcribe_with_whisper(
        Path("/video.mp4"), "small", "en_us", runtime_config=config
    )

    assert calls["path"] == str(audio)
    assert calls["word_timestamps"] is True
    assert calls["language"] == "en"
    assert result["text"] == "Hello world."


def test_whisper_result_keeps_word_level_timestamps_in_milliseconds(whisper_env):
    config, _ = whisper_env
    result = {
        "text": "Hi there",
        "language": "en",
        "segments": [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "Hi there",
                "words": [
                    {"word": "Hi", "start": 1.0, "end": 1.4, "probability": 0.9},
                    {"word": "there", "start": 1.4, "end": 2.0, "probability": 0.8},
                ],
            }
        ],
    }

    cache = transcription._whisper_result_to_transcript_data(result)

    assert [word["start"] for word in cache["words"]] == [1000, 1400]
    assert [word["end"] for word in cache["words"]] == [1400, 2000]
    assert cache["language_code"] == "en"
    assert cache["utterances"][0]["start"] == 1000


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------


def _routing_config(monkeypatch, **overrides):
    base = dict(
        transcription_provider="whisper",
        offline_mode=True,
        allow_hosted_ai=False,
        assembly_ai_api_key=None,
        transcription_language="",
        whisper_model="small",
        whisper_device="cpu",
        whisper_model_dir="models/whisper",
    )
    base.update(overrides)
    config = SimpleNamespace(**base)
    monkeypatch.setattr(transcription, "get_config", lambda: config)
    return config


def test_default_provider_is_local_whisper(monkeypatch):
    _routing_config(monkeypatch)
    called = {}

    def fake_whisper(path, cfg):
        called["whisper"] = True
        return "transcript"

    monkeypatch.setattr(transcription, "_get_transcript_with_whisper", fake_whisper)

    assert transcription.get_video_transcript(Path("/v.mp4")) == "transcript"
    assert called == {"whisper": True}


def test_assemblyai_is_refused_while_offline(monkeypatch):
    _routing_config(monkeypatch, transcription_provider="assemblyai", assembly_ai_api_key="key")
    monkeypatch.setattr(
        transcription, "_get_transcript_with_assemblyai",
        lambda *a, **k: pytest.fail("AssemblyAI must not be called in offline mode"),
    )

    with pytest.raises(RuntimeError, match="offline"):
        transcription.get_video_transcript(Path("/v.mp4"))


def test_assemblyai_without_a_key_raises_instead_of_silently_continuing(monkeypatch):
    _routing_config(
        monkeypatch,
        transcription_provider="assemblyai",
        offline_mode=False,
        allow_hosted_ai=True,
    )

    with pytest.raises(RuntimeError, match="ASSEMBLY_AI_API_KEY"):
        transcription.get_video_transcript(Path("/v.mp4"))


def test_unknown_provider_raises(monkeypatch):
    _routing_config(monkeypatch, transcription_provider="whisper-turbo")

    with pytest.raises(ValueError, match="Unsupported TRANSCRIPTION_PROVIDER"):
        transcription.get_video_transcript(Path("/v.mp4"))


# ---------------------------------------------------------------------------
# Cached word timings: never re-run Whisper just because the AI step failed
# ---------------------------------------------------------------------------


_WHISPER_SAMPLE_RESULT = {
    "text": " Hello world. ",
    "language": "en",
    "segments": [
        {
            "start": 0.0,
            "end": 1.5,
            "text": "Hello world.",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.7, "probability": 0.9},
                {"word": "world.", "start": 0.7, "end": 1.5, "probability": 0.8},
            ],
        }
    ],
}


def _video_file(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    return video


def test_cache_records_the_whisper_model_that_produced_it(tmp_path):
    video = _video_file(tmp_path)

    transcription.cache_transcript_data(video, _WHISPER_SAMPLE_RESULT, whisper_model="small")

    payload = transcription.load_cached_transcript_data(video)
    assert payload["whisper_model"] == "small"
    assert payload["words"][0]["start"] == 0


def test_cached_transcript_is_reused_instead_of_transcribing_again(tmp_path):
    from types import SimpleNamespace

    video = _video_file(tmp_path)
    transcription.cache_transcript_data(video, _WHISPER_SAMPLE_RESULT, whisper_model="small")
    config = SimpleNamespace(whisper_model="small", transcription_language="")

    def boom(*_args, **_kwargs):
        raise AssertionError("Whisper must not run again when a cache exists")

    original = transcription.transcribe_with_whisper
    transcription.transcribe_with_whisper = boom
    try:
        text = transcription._get_transcript_with_whisper(video, config)
    finally:
        transcription.transcribe_with_whisper = original

    assert "Hello world." in text
    # Timestamps are rebuilt from the cached word timings (0.0 s - 1.5 s).
    assert "[00:00 - 00:01] Hello world." in text


def test_switching_model_size_transcribes_again(tmp_path, monkeypatch):
    from types import SimpleNamespace

    video = _video_file(tmp_path)
    transcription.cache_transcript_data(video, _WHISPER_SAMPLE_RESULT, whisper_model="base")
    config = SimpleNamespace(whisper_model="small", transcription_language="")
    calls = []

    def fake_transcribe(*_args, **_kwargs):
        calls.append(True)
        return _WHISPER_SAMPLE_RESULT

    monkeypatch.setattr(transcription, "transcribe_with_whisper", fake_transcribe)

    transcription._get_transcript_with_whisper(video, config)

    assert calls == [True]
    assert transcription.load_cached_transcript_data(video)["whisper_model"] == "small"


def test_a_failed_ai_analysis_keeps_the_transcript_for_the_retry(tmp_path):
    import asyncio

    from src import ai

    video = _video_file(tmp_path)
    transcription.cache_transcript_data(video, _WHISPER_SAMPLE_RESULT, whisper_model="small")

    async def analysis_times_out():
        agent = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("model down")))
        with pytest.raises(RuntimeError):
            await ai._run_transcript_analysis(agent, "prompt", timeout_seconds=5)

    asyncio.run(analysis_times_out())

    # The cached words survive the failed analysis, so the retry does not pay
    # for another transcription.
    payload = transcription.load_cached_transcript_data(video)
    assert payload["words"], "transcript cache must survive a failed AI analysis"
    reused = transcription.reusable_cached_whisper_result(video, "small", None)
    assert reused is not None
    assert reused["segments"][0]["text"] == "Hello world."
