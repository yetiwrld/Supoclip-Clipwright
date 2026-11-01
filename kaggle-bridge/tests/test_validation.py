"""Output-validation tests: the pure checks and the report aggregation."""

from __future__ import annotations

import pytest

from processor import validation


def test_duration_tolerance_has_a_floor_for_short_clips():
    assert validation.duration_tolerance(20) == pytest.approx(0.75)
    assert validation.duration_tolerance(300) == pytest.approx(3.0)


def test_compare_duration_accepts_frame_rounding_and_rejects_a_real_shortfall():
    assert validation.compare_duration(30.0, 29.97).ok
    assert not validation.compare_duration(30.0, 25.0).ok


def test_compare_duration_rejects_non_positive_expectation():
    check = validation.compare_duration(0, 1.0)
    assert not check.ok


def test_fps_matches_exact_and_ntsc_rates():
    assert validation.fps_matches(30.0, 30).ok
    assert validation.fps_matches(29.97, 29.97).ok
    assert not validation.fps_matches(25.0, 30).ok


def test_resolution_matches_requires_both_axes():
    assert validation.resolution_matches((1080, 1920), (1080, 1920)).ok
    assert not validation.resolution_matches((1920, 1080), (1080, 1920)).ok


def test_av_sync_within_and_beyond_tolerance():
    assert validation.av_sync_check(30.00, 30.05).ok
    assert not validation.av_sync_check(30.00, 31.00).ok


def test_frame_pacing_flags_missing_frames():
    # 30s at 30fps is 900 frames; 600 is a visibly truncated file.
    assert validation.frame_pacing_check(900, 30.0, 30).ok
    assert not validation.frame_pacing_check(600, 30.0, 30).ok


def test_silence_check_rejects_digital_silence():
    assert validation.silence_check(-18.0).ok
    assert not validation.silence_check(-91.0).ok
    assert not validation.silence_check(None).ok


def test_parse_fraction_handles_ffprobe_rate_strings():
    assert validation.parse_fraction("30000/1001") == pytest.approx(29.97002997, rel=1e-6)
    assert validation.parse_fraction("30/1") == pytest.approx(30.0)
    assert validation.parse_fraction("0/0") is None
    assert validation.parse_fraction(None) is None


def test_report_ok_ignores_failed_warnings_but_not_errors():
    report = validation.ValidationReport()
    report.add(validation.Check("duration", True, "fine"))
    report.add(validation.Check("fps", False, "off by a hair", severity="warning"))
    assert report.ok
    assert report.warnings and not report.failures

    report.add(validation.Check("opens", False, "cannot decode"))
    assert not report.ok
    assert [c.name for c in report.failures] == ["opens"]


def test_summarise_merges_reports():
    first = validation.ValidationReport()
    first.add(validation.Check("a", True, "ok"))
    second = validation.ValidationReport()
    second.add(validation.Check("b", False, "bad"))
    summary = validation.summarise([first, second])
    assert summary["ok"] is False
    assert summary["checked"] == 2
    assert summary["failed"][0]["name"] == "b"


def test_stream_summary_reads_an_ffprobe_payload():
    payload = {
        "format": {"duration": "12.5", "format_name": "mov,mp4,m4a"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
             "avg_frame_rate": "30/1", "duration": "12.5", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
             "channels": 2, "duration": "12.4"},
        ],
    }
    summary = validation.stream_summary(payload)
    assert summary["has_video"] and summary["has_audio"]
    assert summary["video"]["width"] == 1080
    assert summary["video"]["fps"] == pytest.approx(30.0)
    assert summary["audio"]["channels"] == 2
    assert summary["container_duration"] == pytest.approx(12.5)


def test_stream_summary_handles_a_missing_audio_stream():
    payload = {"format": {"duration": "3"}, "streams": [{"codec_type": "video", "width": 2, "height": 2}]}
    summary = validation.stream_summary(payload)
    assert summary["has_video"] and not summary["has_audio"]


def test_run_output_checks_reports_a_missing_file(tmp_path):
    report = validation.run_output_checks(tmp_path / "nope.mp4")
    assert not report.ok
    assert report.failures[0].name == "file_exists"


def test_run_output_checks_reports_an_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    report = validation.run_output_checks(empty)
    assert not report.ok
    assert report.failures[0].name == "file_not_empty"
