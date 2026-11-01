"""Gate B tests: the transcript cache conversion must match production exactly."""

from __future__ import annotations

from processor import transcription


def test_to_transcript_cache_converts_seconds_to_milliseconds():
    whisper_result = {
        "text": " Hello world. ",
        "language": "en",
        "segments": [
            {
                "start": 1.25,
                "end": 3.5,
                "text": " Hello world.",
                "words": [
                    {"word": " Hello", "start": 1.25, "end": 2.0, "probability": 0.91},
                    {"word": " world.", "start": 2.0, "end": 3.5, "probability": 0.88},
                ],
            }
        ],
    }
    cache = transcription.to_transcript_cache(whisper_result)

    assert cache["version"] == 2
    assert cache["language_code"] == "en"
    assert cache["text"] == " Hello world. "
    assert cache["utterances"][0]["start"] == 1250
    assert cache["utterances"][0]["end"] == 3500
    assert cache["utterances"][0]["speaker"] is None
    assert len(cache["words"]) == 2
    assert cache["words"][0]["confidence"] == 0.91
    assert cache["words"][1]["text"] == " world."
    assert all(isinstance(word["start"], int) for word in cache["words"])


def test_to_transcript_cache_handles_an_empty_result():
    cache = transcription.to_transcript_cache({})
    assert cache == {
        "version": 2,
        "words": [],
        "utterances": [],
        "text": "",
        "language_code": None,
    }


def test_word_without_probability_defaults_to_one():
    cache = transcription.to_transcript_cache(
        {"segments": [{"start": 0, "end": 1, "text": "x", "words": [{"word": " x", "start": 0.0, "end": 1.0}]}]}
    )
    assert cache["words"][0]["confidence"] == 1.0


def test_resolve_device_accepts_explicit_values():
    assert transcription.resolve_device("cpu") == "cpu"
    assert transcription.resolve_device("cuda") == "cuda"


def test_missing_source_raises_before_loading_a_model(tmp_path):
    try:
        transcription.transcribe(tmp_path / "missing.mp4")
    except transcription.TranscriptionError as error:
        assert "not found" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected TranscriptionError")


def test_whisper_environment_reports_collision_shape():
    info = transcription.whisper_environment()
    assert isinstance(info["distributions"], list)
    assert "collision" in info
    assert "importable" in info
