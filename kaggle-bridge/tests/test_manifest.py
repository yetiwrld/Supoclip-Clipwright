"""Manifest contract tests: what a job may say, and what a result must prove."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from processor import discovery, manifest

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOB = BRIDGE_ROOT / "config" / "sample-job.json"


def load_sample_job() -> dict:
    return json.loads(SAMPLE_JOB.read_text(encoding="utf-8"))


def test_sample_job_is_valid():
    outcome = manifest.validate_job(load_sample_job())
    assert outcome.ok, outcome.errors
    assert outcome.warnings == []


def test_schema_version_is_enforced():
    job = load_sample_job()
    job["schema_version"] = "clipwright.kaggle.job/0"
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("schema_version" in error for error in outcome.errors)


def test_missing_required_sections_are_all_reported():
    outcome = manifest.validate_job({"schema_version": manifest.JOB_SCHEMA_VERSION})
    assert not outcome.ok
    # one error per missing top-level section, not just the first one found
    for section in ("job_id", "source", "processing", "transcription", "ai", "rendering", "outputs"):
        assert any(section in error for error in outcome.errors), section


def test_youtube_source_requires_a_url():
    job = load_sample_job()
    job["source"] = {"type": "youtube", "filename": "video.mp4"}
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("url" in error for error in outcome.errors)


def test_local_source_rejects_path_traversal():
    job = load_sample_job()
    job["source"]["filename"] = "../../etc/passwd"
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("bare file name" in error for error in outcome.errors)


def test_odd_dimensions_are_rejected():
    job = load_sample_job()
    job["rendering"]["target"]["width"] = 1081
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("even" in error for error in outcome.errors)


def test_unknown_codec_is_rejected():
    job = load_sample_job()
    job["rendering"]["codec"] = "h265_videotoolbox"
    outcome = manifest.validate_job(job)
    assert not outcome.ok


def test_bad_checksum_format_is_rejected():
    job = load_sample_job()
    job["source"]["sha256"] = "NOT-A-HASH"
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("sha256" in error for error in outcome.errors)


@pytest.mark.parametrize(
    "key",
    ["bridge_token", "kaggle_key", "api_key", "hf_token", "password", "aws_secret_access_key"],
)
def test_credential_shaped_keys_are_rejected(key):
    job = load_sample_job()
    job["extra"] = {key: "value"}
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("credential-shaped" in error for error in outcome.errors)


def test_credential_shaped_values_are_rejected():
    job = load_sample_job()
    job["source"]["notes"] = "Bearer abcdef123456"
    outcome = manifest.validate_job(job)
    assert not outcome.ok
    assert any("credential" in error for error in outcome.errors)


def test_nested_secret_is_found():
    job = load_sample_job()
    job["ai"]["headers"] = {"X-Api-Key": "abc"}
    outcome = manifest.validate_job(job)
    assert not outcome.ok


def test_load_and_validate_raises_manifest_error(tmp_path: Path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
    with pytest.raises(manifest.ManifestError):
        manifest.load_and_validate_job(path)


def test_sha256_file_matches_known_digest(tmp_path: Path):
    path = tmp_path / "payload.bin"
    path.write_bytes(b"clipwright")
    assert (
        manifest.sha256_file(path)
        == "eef16d398ffb96dbef62c197eb5880f293869cf365e5b08360db4c0594213c45"
    )


def test_output_file_records_size_and_digest(tmp_path: Path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x01\x02")
    record = manifest.OutputFile.from_path(path)
    assert record.size_bytes == 3
    assert len(record.sha256) == 64
    assert record.as_dict()["name"] == "clip.mp4"


def test_build_result_requires_known_status():
    with pytest.raises(ValueError):
        manifest.build_result(job={"job_id": "x"}, status="done", outputs=[])


def test_result_validation_accepts_a_minimal_completed_result():
    result = manifest.build_result(job={"job_id": "job-1"}, status="completed", outputs=[])
    outcome = manifest.validate_result(result)
    assert outcome.ok, outcome.errors


def test_failed_result_must_carry_an_error():
    result = manifest.build_result(job={"job_id": "job-1"}, status="failed", outputs=[])
    outcome = manifest.validate_result(result)
    assert not outcome.ok
    assert any("error" in error for error in outcome.errors)


def test_result_rejects_bad_checksum_and_empty_output():
    result = manifest.build_result(
        job={"job_id": "job-1"},
        status="completed",
        outputs=[],
    )
    result["outputs"] = [{"name": "a.mp4", "path": "/tmp/a.mp4", "size_bytes": 0, "sha256": "zzz"}]
    outcome = manifest.validate_result(result)
    assert not outcome.ok
    assert any("empty" in error for error in outcome.errors)
    assert any("sha256" in error for error in outcome.errors)


def test_verify_outputs_passes_and_detects_tampering(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"real-bytes")
    record = manifest.OutputFile.from_path(clip)
    result = manifest.build_result(job={"job_id": "job-1"}, status="completed", outputs=[record])

    assert manifest.verify_outputs(result).ok

    clip.write_bytes(b"tampered")
    outcome = manifest.verify_outputs(result)
    assert not outcome.ok
    assert any("mismatch" in error for error in outcome.errors)


def test_verify_outputs_detects_missing_file(tmp_path: Path):
    record = manifest.OutputFile("clip.mp4", str(tmp_path / "clip.mp4"), 10, "a" * 64)
    result = manifest.build_result(job={"job_id": "job-1"}, status="completed", outputs=[record])
    outcome = manifest.verify_outputs(result)
    assert not outcome.ok
    assert any("missing file" in error for error in outcome.errors)


def test_load_json_tolerates_a_utf8_bom(tmp_path: Path):
    """Windows PowerShell 5.1 writes a BOM; the reader must not choke on it."""
    path = tmp_path / "job.json"
    payload = json.dumps(load_sample_job())
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    loaded = manifest.load_json(path)
    assert loaded["job_id"] == load_sample_job()["job_id"]
    assert manifest.validate_job(loaded).ok

    discovered = discovery.find_job_manifest(
        working_dirs=[tmp_path], input_root=None
    )
    assert discovered == path
