"""Gate C tests: prompt fidelity, JSON extraction and the output contract."""

from __future__ import annotations

import json

import pytest

from processor import ai_inference


def test_prompts_are_bit_identical_to_production_expectations():
    system = ai_inference.build_system_prompt()
    # Placeholders must be substituted, and the contract language must survive.
    assert "{minimum}" not in system
    assert "15-60 seconds" in system
    for phrase in (
        'The top-level JSON object must include: "most_relevant_segments", "summary", and "key_topics"',
        "Return valid JSON only.",
        "HOOK TITLES",
        "CONTENT NEUTRALITY RULES",
    ):
        assert phrase in system


def test_user_prompt_keeps_the_production_sections_and_bounds():
    prompt = ai_inference.build_transcript_user_prompt(
        "[00:01 - 00:20] Hello there.", bounds=ai_inference.ClipBounds()
    )
    assert "Transcript:\n[00:01 - 00:20] Hello there." in prompt
    assert "JSON-only output requirements:" in prompt
    assert "Choose 2-5 segments total." in prompt
    assert "25-50 seconds" in prompt


def test_user_prompt_includes_a_window_note_only_when_windowed():
    without = ai_inference.build_transcript_user_prompt("x")
    with_window = ai_inference.build_transcript_user_prompt("x", window=(600, 1800))
    assert "one window" not in without
    assert "one window (10:00 - 30:00)" in with_window


def test_format_transcript_lines_matches_production_shape():
    cache = {
        "utterances": [
            {"text": " Hello world ", "start": 1000, "end": 2500, "speaker": None},
            {"text": "", "start": 2600, "end": 3000, "speaker": None},
            {"text": "Second line", "start": 3000, "end": 4500, "speaker": "A"},
        ]
    }
    assert ai_inference.format_transcript_lines(cache) == [
        "[00:01 - 00:02] Hello world",
        "[00:03 - 00:04] Speaker A: Second line",
    ]


def test_ms_timestamps_use_unbounded_minutes():
    assert ai_inference.format_ms_to_timestamp(5_000) == "00:05"
    assert ai_inference.format_ms_to_timestamp(75 * 60 * 1000) == "75:00"


def test_extract_json_object_handles_fences_and_prose():
    raw = 'Here you go:\n```json\n{"most_relevant_segments": [], "summary": "s", "key_topics": []}\n```'
    assert ai_inference.extract_json_object(raw)["summary"] == "s"


def test_extract_json_object_ignores_braces_inside_strings():
    raw = '{"summary": "he said {not json}", "key_topics": ["a"]}'
    assert ai_inference.extract_json_object(raw)["summary"] == "he said {not json}"


def test_extract_json_object_rejects_garbage():
    with pytest.raises(ai_inference.AnalysisError):
        ai_inference.extract_json_object("I cannot help with that.")


def _valid_payload() -> dict:
    return {
        "most_relevant_segments": [
            {
                "start_time": "00:10",
                "end_time": "00:45",
                "text": "The whole point, verbatim.",
                "relevance_score": 0.9,
                "reasoning": "Self-contained claim with a payoff.",
                "virality": {
                    "hook_score": 20,
                    "engagement_score": 18,
                    "value_score": 22,
                    "shareability_score": 19,
                    "total_score": 79,
                    "hook_type": "statement",
                    "virality_reasoning": "Opens on a concrete claim.",
                },
                "hook_title": "Why nobody tells you this",
            }
        ],
        "summary": "A talk about clipping.",
        "key_topics": ["editing", "distribution"],
    }


def test_validate_analysis_accepts_the_contract():
    result = ai_inference.validate_analysis(_valid_payload())
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment["start_seconds"] == 10
    assert segment["end_seconds"] == 45
    assert segment["duration_seconds"] == 35
    assert segment["virality"]["total_score"] == 79
    assert result.summary.startswith("A talk")


def test_validate_analysis_defaults_missing_virality_like_pydantic():
    payload = _valid_payload()
    del payload["most_relevant_segments"][0]["virality"]
    result = ai_inference.validate_analysis(payload)
    assert result.segments[0]["virality"] == ai_inference._default_virality()


def test_validate_analysis_coerces_percent_relevance():
    payload = _valid_payload()
    payload["most_relevant_segments"][0]["relevance_score"] = 85
    result = ai_inference.validate_analysis(payload)
    assert result.segments[0]["relevance_score"] == pytest.approx(0.85)


def test_short_segment_is_rejected_with_a_reason():
    payload = _valid_payload()
    payload["most_relevant_segments"][0]["end_time"] = "00:20"  # 10s, below the 15s minimum
    result = ai_inference.validate_analysis(payload)
    assert result.segments == []
    assert "shorter than" in result.rejected[0]["reason"]


def test_inverted_timestamps_are_rejected():
    payload = _valid_payload()
    payload["most_relevant_segments"][0].update({"start_time": "00:45", "end_time": "00:10"})
    result = ai_inference.validate_analysis(payload)
    assert result.segments == []
    assert "not after" in result.rejected[0]["reason"]


def test_segment_past_the_source_end_is_rejected():
    payload = _valid_payload()
    result = ai_inference.validate_analysis(payload, source_duration_seconds=20)
    assert result.segments == []
    assert "past the" in result.rejected[0]["reason"]


def test_text_alias_is_accepted():
    payload = _valid_payload()
    segment = payload["most_relevant_segments"][0]
    segment["segment"] = segment.pop("text")
    result = ai_inference.validate_analysis(payload)
    assert result.segments[0]["text"] == "The whole point, verbatim."


def test_max_segments_keeps_the_highest_scoring_clips():
    payload = _valid_payload()
    second = json.loads(json.dumps(payload["most_relevant_segments"][0]))
    second["start_time"], second["end_time"] = "01:00", "01:40"
    second["virality"]["total_score"] = 40
    payload["most_relevant_segments"].append(second)
    result = ai_inference.validate_analysis(payload, max_segments=1)
    assert len(result.segments) == 1
    assert result.segments[0]["virality"]["total_score"] == 79


def test_missing_segment_array_raises():
    with pytest.raises(ai_inference.AnalysisError):
        ai_inference.validate_analysis({"summary": "x", "key_topics": []})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"#hashtag Claim"', "Claim"),
        ("  spaced   out  ", "spaced out"),
        ("Ends with a full stop.", "Ends with a full stop"),
        (None, None),
        ("   ", None),
    ],
)
def test_hook_title_sanitiser(raw, expected):
    assert ai_inference.sanitize_hook_title(raw) == expected


def test_hook_title_is_capped_to_ten_words():
    raw = "one two three four five six seven eight nine ten eleven twelve"
    assert len(ai_inference.sanitize_hook_title(raw).split()) == 10


def test_build_backend_reports_every_backend_it_tried(monkeypatch):
    monkeypatch.setattr(ai_inference.OllamaBackend, "available", lambda self: (False, "no daemon"))
    monkeypatch.setattr(ai_inference.TransformersBackend, "available", lambda self: (False, "no torch"))
    with pytest.raises(ai_inference.AnalysisError) as error:
        ai_inference.build_backend(backend="auto")
    assert "no daemon" in str(error.value) and "no torch" in str(error.value)
