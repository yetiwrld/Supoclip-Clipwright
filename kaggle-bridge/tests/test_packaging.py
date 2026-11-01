"""Packaging + metadata safeguards: the logic the .ps1 scripts delegate to."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from processor import packaging


# --------------------------------------------------------------------------- #
# size limits (the Compress-Archive 2 GB problem)
# --------------------------------------------------------------------------- #


def test_small_input_has_no_warning():
    assert packaging.check_input_size(10 * 1024 * 1024) is None


def test_large_but_allowed_input_warns():
    warning = packaging.check_input_size(int(1.5 * 1024**3))
    assert warning and "short clip" in warning


def test_oversize_input_fails_before_packaging_and_says_how_to_raise_it():
    with pytest.raises(packaging.PackagingError) as error:
        packaging.check_input_size(int(5 * 1024**3))
    message = str(error.value)
    assert "5.00 GB" in message and "4.00 GB" in message
    assert "MaxInputGb" in message


def test_custom_ceiling_is_honoured():
    packaging.check_input_size(150 * 1024 * 1024, max_bytes=200 * 1024 * 1024)
    with pytest.raises(packaging.PackagingError):
        packaging.check_input_size(300 * 1024 * 1024, max_bytes=200 * 1024 * 1024)


def test_documented_limits_are_ordered_sensibly():
    assert packaging.RECOMMENDED_MAX_INPUT_BYTES < packaging.DEFAULT_MAX_INPUT_BYTES
    assert packaging.COMPRESS_ARCHIVE_LIMIT_BYTES == 2 * 1024**3
    assert packaging.KAGGLE_AUTOSAVE_BYTES == 20 * 1024**3


def test_human_bytes_formats_units():
    assert packaging.human_bytes(512) == "512 B"
    assert packaging.human_bytes(2048) == "2.00 KB"
    assert packaging.human_bytes(4 * 1024**3) == "4.00 GB"


# --------------------------------------------------------------------------- #
# zip building
# --------------------------------------------------------------------------- #


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "stage"
    (root / "processor").mkdir(parents=True)
    (root / "processor" / "__init__.py").write_text("", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "sample-job.json").write_text("{}", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "junk.pyc").write_bytes(b"junk")
    (root / "processor" / "__pycache__").mkdir()
    (root / "processor" / "__pycache__" / "more.pyc").write_bytes(b"junk")
    return root


def test_tree_zip_keeps_arc_root_and_drops_caches(tmp_path):
    root = _tree(tmp_path)
    data = packaging.build_tree_zip(root, tmp_path / "code.zip", arc_root="kaggle-bridge")
    assert "kaggle-bridge/processor/__init__.py" in data["names"]
    assert "kaggle-bridge/config/sample-job.json" in data["names"]
    assert not [name for name in data["names"] if "__pycache__" in name or name.endswith(".pyc")]


def test_tree_zip_without_arc_root_stores_relative_paths(tmp_path):
    root = _tree(tmp_path)
    data = packaging.build_tree_zip(root, tmp_path / "code.zip")
    assert data["names"] == sorted(data["names"])
    assert "processor/__init__.py" in data["names"]


def test_tree_zip_refuses_an_empty_directory(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(packaging.PackagingError):
        packaging.build_tree_zip(tmp_path / "empty", tmp_path / "x.zip")


def test_tree_zip_refuses_to_follow_symlinks(tmp_path):
    root = _tree(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks unavailable on this filesystem")
    with pytest.raises(packaging.PackagingError) as error:
        packaging.build_tree_zip(root, tmp_path / "x.zip")
    assert "symlink" in str(error.value)


def test_file_zip_writes_the_video_and_the_job(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 1024)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema_version": "clipwright.kaggle.job/1"}), encoding="utf-8")

    data = packaging.build_file_zip({"clip.mp4": video, "job.json": job}, tmp_path / "in.zip")
    assert data["entries"] == 2
    assert sorted(data["names"]) == ["clip.mp4", "job.json"]


def test_file_zip_rejects_a_missing_file(tmp_path):
    with pytest.raises(packaging.PackagingError):
        packaging.build_file_zip({"x.mp4": tmp_path / "absent.mp4"}, tmp_path / "x.zip")


@pytest.mark.parametrize("arcname", ["../escape.mp4", "/absolute.mp4"])
def test_file_zip_rejects_unsafe_archive_names(tmp_path, arcname):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(packaging.PackagingError):
        packaging.build_file_zip({arcname: video}, tmp_path / "x.zip")


def test_zip_is_written_with_zip64_enabled(tmp_path, monkeypatch):
    """ZIP64 must be requested even for small archives, or >4 GB entries break."""
    seen = {}
    real_zipfile = zipfile.ZipFile

    class Spy(real_zipfile):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(packaging.zipfile, "ZipFile", Spy)
    root = _tree(tmp_path)
    packaging.build_tree_zip(root, tmp_path / "code.zip")
    assert seen.get("allowZip64") is True
    assert seen.get("compression") == zipfile.ZIP_DEFLATED


def test_disabling_zip64_fails_a_2gb_entry_instead_of_writing_a_broken_archive(tmp_path):
    """The old Compress-Archive ceiling, enforced explicitly rather than silently."""
    big = tmp_path / "big.mp4"
    with open(big, "wb") as handle:
        handle.truncate(2 * 1024**3 + 1024)  # sparse: no disk cost
    with pytest.raises(packaging.PackagingError) as error:
        packaging.build_file_zip({"big.mp4": big}, tmp_path / "out.zip", allow_zip64=False)
    assert "ZIP64" in str(error.value)


def test_oversize_check_is_cheap_for_a_sparse_4gb_file(tmp_path):
    big = tmp_path / "big.mp4"
    with open(big, "wb") as handle:
        handle.truncate(5 * 1024**3)
    with pytest.raises(packaging.PackagingError):
        packaging.check_input_size(big.stat().st_size)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def test_verify_zip_reports_checksums_and_sizes(tmp_path):
    root = _tree(tmp_path)
    packaging.build_tree_zip(root, tmp_path / "code.zip", arc_root="kaggle-bridge")
    source_bytes = sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    summary = packaging.zip_summary(tmp_path / "code.zip")
    assert summary.entries >= 2
    assert summary.sha256 == packaging.file_sha256(tmp_path / "code.zip")
    assert summary.uncompressed_bytes == source_bytes
    assert summary.compressed_bytes > 0
    assert summary.as_dict()["compressed"].endswith(("KB", "B"))


def test_verify_zip_rejects_a_corrupt_archive(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(packaging.PackagingError):
        packaging.verify_zip(broken)


def test_verify_zip_rejects_an_empty_archive(tmp_path):
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    with pytest.raises(packaging.PackagingError):
        packaging.verify_zip(empty)


def test_verify_zip_rejects_path_traversal_entries(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escape.txt", "boom")
    with pytest.raises(packaging.PackagingError) as error:
        packaging.verify_zip(evil)
    assert "unsafe" in str(error.value)


# --------------------------------------------------------------------------- #
# kernel metadata
# --------------------------------------------------------------------------- #


def valid_metadata(**overrides) -> dict:
    metadata = {
        "id": "someone/clipwright-bridge-phase1",
        "title": "ClipWright Bridge Phase 1",
        "code_file": "clipwright_worker.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "machine_shape": "NvidiaTeslaT4",
        "dataset_sources": ["someone/clipwright-bridge-code", "someone/clipwright-phase1-input"],
        "kernel_sources": [],
        "competition_sources": [],
    }
    metadata.update(overrides)
    return metadata


@pytest.fixture()
def kernel_dir(tmp_path):
    (tmp_path / "clipwright_worker.py").write_text("# worker\n", encoding="utf-8")
    return tmp_path


def fields(report: packaging.MetadataReport) -> list[str]:
    return [issue.field for issue in report.issues]


def test_valid_metadata_passes(kernel_dir):
    report = packaging.validate_kernel_metadata(valid_metadata(), kernel_dir=kernel_dir)
    assert report.ok, [str(issue) for issue in report.issues]
    assert report.warnings == []


def test_public_kernel_is_an_error(kernel_dir):
    report = packaging.validate_kernel_metadata(valid_metadata(is_private=False), kernel_dir=kernel_dir)
    assert not report.ok and "is_private" in fields(report)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_private_must_be_a_real_true(kernel_dir, value):
    report = packaging.validate_kernel_metadata(valid_metadata(is_private=value), kernel_dir=kernel_dir)
    assert not report.ok


def test_gpu_must_be_enabled(kernel_dir):
    report = packaging.validate_kernel_metadata(valid_metadata(enable_gpu=False), kernel_dir=kernel_dir)
    assert not report.ok and "enable_gpu" in fields(report)


def test_machine_shape_must_be_the_t4(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(machine_shape="NvidiaTeslaP100"), kernel_dir=kernel_dir
    )
    assert not report.ok and "machine_shape" in fields(report)


def test_missing_machine_shape_is_reported(kernel_dir):
    metadata = valid_metadata()
    del metadata["machine_shape"]
    report = packaging.validate_kernel_metadata(metadata, kernel_dir=kernel_dir)
    assert "machine_shape" in fields(report)


def test_missing_internet_key_is_an_error_with_a_reason(kernel_dir):
    metadata = valid_metadata()
    del metadata["enable_internet"]
    report = packaging.validate_kernel_metadata(metadata, kernel_dir=kernel_dir)
    assert "enable_internet" in fields(report)
    assert "openai-whisper" in str(report.errors[0])


def test_internet_disabled_is_an_error(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(enable_internet=False), kernel_dir=kernel_dir
    )
    assert not report.ok and "enable_internet" in fields(report)


@pytest.mark.parametrize("datasets", [[], ["someone/only-code"], ["a/b", "a/c", "a/d"]])
def test_dataset_count_must_be_exactly_two(kernel_dir, datasets):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=datasets), kernel_dir=kernel_dir
    )
    assert not report.ok and "dataset_sources" in fields(report)


def test_duplicate_datasets_are_rejected(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=["someone/code", "someone/code"]), kernel_dir=kernel_dir
    )
    assert not report.ok


def test_dataset_sources_must_be_a_list(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources="someone/code"), kernel_dir=kernel_dir
    )
    assert not report.ok


@pytest.mark.parametrize(
    "kernel_id",
    [
        "<your-kaggle-username>/clipwright-bridge-phase1",
        "your-kaggle-username/clipwright",
        "changeme/clipwright",
        "no-slash",
        "",
    ],
)
def test_placeholder_or_malformed_kernel_ids_are_rejected(kernel_dir, kernel_id):
    report = packaging.validate_kernel_metadata(valid_metadata(id=kernel_id), kernel_dir=kernel_dir)
    assert not report.ok and "id" in fields(report)


def test_dataset_placeholder_is_rejected(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=["<your-kaggle-username>/code", "someone/input"]),
        kernel_dir=kernel_dir,
    )
    assert not report.ok


def test_dataset_slug_case_is_rejected(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=["someone/Clipwright-Code", "someone/input"]),
        kernel_dir=kernel_dir,
    )
    assert not report.ok


def test_dataset_that_is_the_kernel_itself_is_rejected(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=["someone/clipwright-bridge-phase1", "someone/input"]),
        kernel_dir=kernel_dir,
    )
    assert not report.ok


def test_missing_worker_file_is_an_error(kernel_dir):
    (kernel_dir / "clipwright_worker.py").unlink()
    report = packaging.validate_kernel_metadata(valid_metadata(), kernel_dir=kernel_dir)
    assert "code_file" in fields(report)


def test_wrong_code_file_name_is_an_error(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(code_file="worker.py"), kernel_dir=kernel_dir
    )
    assert "code_file" in fields(report)


def test_non_script_kernel_type_is_an_error(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(kernel_type="notebook"), kernel_dir=kernel_dir
    )
    assert "kernel_type" in fields(report)


def test_unexpected_kernel_sources_are_rejected(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(kernel_sources=["someone/other-kernel"]), kernel_dir=kernel_dir
    )
    assert "kernel_sources" in fields(report)


def test_missing_title_is_only_a_warning(kernel_dir):
    metadata = valid_metadata()
    del metadata["title"]
    report = packaging.validate_kernel_metadata(metadata, kernel_dir=kernel_dir)
    assert report.ok
    assert "title" in [issue.field for issue in report.warnings]


def test_dataset_owned_by_another_account_warns(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(dataset_sources=["someone/code", "other/input"]), kernel_dir=kernel_dir
    )
    assert report.ok
    assert report.warnings


def test_all_problems_are_reported_in_one_pass(kernel_dir):
    report = packaging.validate_kernel_metadata(
        valid_metadata(is_private=False, enable_gpu=False, machine_shape="CPU"),
        kernel_dir=kernel_dir,
    )
    assert len(report.errors) >= 3


def test_shipped_metadata_is_not_pushable_yet():
    root = Path(__file__).resolve().parents[1]
    metadata = packaging.load_kernel_metadata(root / "kernel" / "kernel-metadata.json")
    report = packaging.validate_kernel_metadata(metadata, kernel_dir=root / "kernel")
    assert not report.ok  # placeholders until the operator fills in their username
    assert all("placeholder" in str(issue) for issue in report.errors)


def test_load_kernel_metadata_rejects_bad_json(tmp_path):
    path = tmp_path / "kernel-metadata.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(packaging.PackagingError):
        packaging.load_kernel_metadata(path)


def test_load_kernel_metadata_reports_a_missing_file(tmp_path):
    with pytest.raises(packaging.PackagingError):
        packaging.load_kernel_metadata(tmp_path / "absent.json")


# --------------------------------------------------------------------------- #
# CLI surface used by the .ps1 scripts
# --------------------------------------------------------------------------- #


def test_cli_check_metadata_exit_codes(kernel_dir, capsys):
    good = kernel_dir / "good.json"
    good.write_text(json.dumps(valid_metadata()), encoding="utf-8")
    assert packaging.main(["--json", "check-metadata", "--metadata", str(good), "--kernel-dir", str(kernel_dir)]) == 0

    bad = kernel_dir / "bad.json"
    bad.write_text(json.dumps(valid_metadata(is_private=False)), encoding="utf-8")
    assert packaging.main(["check-metadata", "--metadata", str(bad), "--kernel-dir", str(kernel_dir)]) == 1
    assert "is_private" in capsys.readouterr().out


def test_cli_strict_turns_warnings_into_failure(kernel_dir):
    metadata = valid_metadata()
    del metadata["title"]
    path = kernel_dir / "warn.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert packaging.main(["check-metadata", "--metadata", str(path), "--kernel-dir", str(kernel_dir)]) == 0
    assert packaging.main(["check-metadata", "--metadata", str(path), "--kernel-dir", str(kernel_dir), "--strict"]) == 1


def test_cli_zip_input_refuses_an_oversize_video(tmp_path):
    video = tmp_path / "huge.mp4"
    with open(video, "wb") as handle:
        handle.truncate(3 * 1024**3)
    job = tmp_path / "job.json"
    job.write_text("{}", encoding="utf-8")
    destination = tmp_path / "input.zip"
    code = packaging.main(
        [
            "zip-input",
            "--video", str(video),
            "--job", str(job),
            "--destination", str(destination),
            "--max-gb", "2",
        ]
    )
    assert code == 1
    assert not destination.exists(), "nothing should be written after the guard fails"


def test_cli_zip_input_roundtrip(tmp_path, capsys):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 512)
    job = tmp_path / "job.json"
    job.write_text("{}", encoding="utf-8")
    destination = tmp_path / "input.zip"
    assert packaging.main(
        ["zip-input", "--video", str(video), "--job", str(job), "--destination", str(destination)]
    ) == 0
    assert destination.exists()
    assert packaging.main(["verify-zip", "--path", str(destination)]) == 0
    assert "verified" in capsys.readouterr().out


def test_cli_limits_prints_both_ceiling_and_zip64(capsys):
    assert packaging.main(["limits"]) == 0
    out = capsys.readouterr().out
    assert "4.00 GB" in out and "ZIP64" in out.replace("zip64", "ZIP64")
