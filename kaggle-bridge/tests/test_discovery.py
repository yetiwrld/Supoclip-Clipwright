"""Discovery tests: a fake Kaggle mount tree, not a real notebook.

These encode the layout Kaggle actually produces — ``/kaggle/input/<dataset-slug>/``
containing the archive's top-level folder — so a change in the kernel's bootstrap is
caught here instead of at hour one of a GPU session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from processor import discovery


def make_bridge(root: Path) -> Path:
    """Create a minimal bridge package under ``root``."""
    package = root / "processor"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "manifest.py").write_text("", encoding="utf-8")
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "sample-job.json").write_text("{}", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# the code dataset
# --------------------------------------------------------------------------- #


def test_finds_the_package_one_level_below_the_dataset_root(tmp_path):
    # /kaggle/input/clipwright-bridge-code/kaggle-bridge/processor
    make_bridge(tmp_path / "clipwright-bridge-code" / "kaggle-bridge")
    found = discovery.find_bridge_root(search_roots=[tmp_path / "clipwright-bridge-code"])
    assert found == (tmp_path / "clipwright-bridge-code" / "kaggle-bridge").resolve()


def test_finds_the_package_with_a_double_nested_archive_root(tmp_path):
    # some uploads keep an extra folder: <slug>/<archive-name>/kaggle-bridge/processor
    make_bridge(tmp_path / "clipwright-bridge-code" / "clipwright-bridge" / "kaggle-bridge")
    found = discovery.find_bridge_root(search_roots=[tmp_path / "clipwright-bridge-code"])
    assert found.name == "kaggle-bridge"
    assert discovery.is_bridge_root(found)


def test_finds_the_package_when_the_dataset_root_is_the_package(tmp_path):
    make_bridge(tmp_path / "code-dataset")
    found = discovery.find_bridge_root(search_roots=[tmp_path / "code-dataset"])
    assert found == (tmp_path / "code-dataset").resolve()


def test_shallower_match_wins_over_a_deeper_copy(tmp_path):
    make_bridge(tmp_path / "code" / "kaggle-bridge")
    make_bridge(tmp_path / "code" / "vendor" / "old" / "kaggle-bridge")
    found = discovery.find_bridge_root(search_roots=[tmp_path / "code"])
    assert found == (tmp_path / "code" / "kaggle-bridge").resolve()


def test_override_wins_over_the_search_roots(tmp_path):
    root = make_bridge(tmp_path / "bridge")
    make_bridge(tmp_path / "elsewhere")
    assert (
        discovery.find_bridge_root(override=root, search_roots=[tmp_path / "elsewhere"])
        == root.resolve()
    )


def test_override_may_point_at_a_repo_root(tmp_path):
    # BRIDGE_PATH=<repo> with the package in <repo>/kaggle-bridge is the documented
    # local-development form.
    root = make_bridge(tmp_path / "repo" / "kaggle-bridge")
    assert discovery.find_bridge_root(override=tmp_path / "repo") == root.resolve()


def test_bad_override_is_a_clear_error(tmp_path):
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_bridge_root(override=tmp_path / "nothing")
    assert "BRIDGE_PATH" in str(error.value)


def test_missing_package_lists_what_it_searched(tmp_path):
    (tmp_path / "empty-dataset").mkdir()
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_bridge_root(search_roots=[tmp_path / "empty-dataset"])
    message = str(error.value)
    assert "processor/__init__.py" in message
    assert "empty-dataset" in message


def test_absent_roots_are_skipped_not_fatal(tmp_path):
    make_bridge(tmp_path / "real" / "kaggle-bridge")
    found = discovery.find_bridge_root(
        search_roots=[tmp_path / "does-not-exist", tmp_path / "real"]
    )
    assert found.name == "kaggle-bridge"


def test_caches_are_not_searched(tmp_path):
    package = tmp_path / "code" / "__pycache__" / "kaggle-bridge" / "processor"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(discovery.DiscoveryError):
        discovery.find_bridge_root(search_roots=[tmp_path / "code"])


def test_depth_limit_is_respected(tmp_path):
    make_bridge(tmp_path / "code" / "a" / "b" / "c" / "d" / "kaggle-bridge")
    with pytest.raises(discovery.DiscoveryError):
        discovery.find_bridge_root(search_roots=[tmp_path / "code"], max_depth=2)


# --------------------------------------------------------------------------- #
# the job manifest
# --------------------------------------------------------------------------- #


def _write_job(path: Path, schema: str = "clipwright.kaggle.job/1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": schema, "job_id": "x"}), encoding="utf-8")
    return path


def test_working_directory_job_wins_over_the_dataset(tmp_path):
    working = tmp_path / "working"
    input_root = tmp_path / "input"
    expected = _write_job(working / "job.json")
    _write_job(input_root / "input-dataset" / "job.json")
    found = discovery.find_job_manifest(working_dirs=[working], input_root=input_root)
    assert found == expected


def test_finds_job_json_inside_the_input_dataset(tmp_path):
    input_root = tmp_path / "input"
    expected = _write_job(input_root / "clipwright-phase1-input" / "job.json")
    found = discovery.find_job_manifest(working_dirs=[], input_root=input_root)
    assert found == expected


def test_json_with_the_wrong_schema_is_ignored(tmp_path):
    input_root = tmp_path / "input"
    _write_job(input_root / "other" / "job.json", schema="something.else/9")
    expected = _write_job(input_root / "real" / "sample-job.json")
    found = discovery.find_job_manifest(
        input_root=input_root,
        is_valid_job=lambda payload: payload.get("schema_version") == "clipwright.kaggle.job/1",
    )
    assert found == expected


def test_unparseable_json_is_skipped(tmp_path):
    input_root = tmp_path / "input"
    broken = input_root / "aa" / "job.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    expected = _write_job(input_root / "zz" / "job.json")
    found = discovery.find_job_manifest(
        input_root=input_root,
        is_valid_job=lambda payload: payload.get("schema_version") == "clipwright.kaggle.job/1",
    )
    assert found == expected


def test_explicit_missing_job_is_reported_immediately(tmp_path):
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_job_manifest(explicit=tmp_path / "nope.json")
    assert "not found" in str(error.value)


def test_no_job_anywhere_explains_what_to_attach(tmp_path):
    input_root = tmp_path / "input"
    (input_root / "empty").mkdir(parents=True)
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_job_manifest(input_root=input_root)
    assert "job.json" in str(error.value)


# --------------------------------------------------------------------------- #
# the input video
# --------------------------------------------------------------------------- #


def test_exact_filename_wins_inside_the_dataset(tmp_path):
    input_root = tmp_path / "input"
    target = input_root / "clipwright-phase1-input" / "sample.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"video-bytes")
    (input_root / "clipwright-phase1-input" / "other.mp4").write_bytes(b"other")
    found = discovery.find_input_media(filename="sample.mp4", input_root=input_root)
    assert found == target


def test_single_video_is_used_even_when_kaggle_renamed_it(tmp_path):
    input_root = tmp_path / "input"
    renamed = input_root / "dataset" / "whatever-kaggle-called-it.mp4"
    renamed.parent.mkdir(parents=True)
    renamed.write_bytes(b"x")
    found = discovery.find_input_media(filename="sample.mp4", input_root=input_root)
    assert found == renamed


def test_several_videos_and_no_name_match_is_an_error_not_a_guess(tmp_path):
    input_root = tmp_path / "input"
    for name in ("a.mp4", "b.mov"):
        path = input_root / "dataset" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_input_media(filename="sample.mp4", input_root=input_root)
    message = str(error.value)
    assert "several videos" in message
    assert "a.mp4" in message and "b.mov" in message


def test_declared_size_disambiguates_several_videos(tmp_path):
    input_root = tmp_path / "input"
    small = input_root / "dataset" / "a.mp4"
    big = input_root / "dataset" / "b.mp4"
    small.parent.mkdir(parents=True)
    small.write_bytes(b"small")
    big.write_bytes(b"much-larger-video")
    found = discovery.find_input_media(
        filename="sample.mp4",
        input_root=input_root,
        declared_size=big.stat().st_size,
    )
    assert found == big


def test_explicit_path_wins(tmp_path):
    explicit = tmp_path / "given.mp4"
    explicit.write_bytes(b"x")
    found = discovery.find_input_media(
        filename="sample.mp4", explicit_path=explicit, input_root=tmp_path / "nope"
    )
    assert found == explicit


def test_missing_video_message_names_the_file_and_location(tmp_path):
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.find_input_media(filename="absent.mp4", input_root=tmp_path)
    message = str(error.value)
    assert "absent.mp4" in message and str(tmp_path) in message


def test_empty_filename_without_path_is_rejected(tmp_path):
    with pytest.raises(discovery.DiscoveryError):
        discovery.find_input_media(filename="", input_root=tmp_path)


def test_sha256_of_matches_manifest_helper(tmp_path):
    from processor import manifest

    path = tmp_path / "payload.bin"
    path.write_bytes(b"clipwright")
    assert discovery.sha256_of(path) == manifest.sha256_file(path)


def test_real_job_beats_the_example_shipped_in_the_code_dataset(tmp_path):
    """The code dataset contains config/sample-job.json; it must never shadow the job."""
    input_root = tmp_path / "input"
    example = _write_job(input_root / "clipwright-bridge-code" / "kaggle-bridge" / "config" / "sample-job.json")
    real = _write_job(input_root / "clipwright-phase1-input" / "job.json")
    assert example.is_file()
    found = discovery.find_job_manifest(
        input_root=input_root,
        is_valid_job=lambda payload: payload.get("schema_version") == "clipwright.kaggle.job/1",
    )
    assert found == real


def test_the_bundled_example_is_still_usable_on_its_own(tmp_path):
    input_root = tmp_path / "input"
    example = _write_job(input_root / "code" / "kaggle-bridge" / "config" / "sample-job.json")
    found = discovery.find_job_manifest(
        input_root=input_root,
        is_valid_job=lambda payload: payload.get("schema_version") == "clipwright.kaggle.job/1",
    )
    assert found == example
