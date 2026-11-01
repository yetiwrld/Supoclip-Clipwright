"""End-to-end discovery: run the *real kernel module* against a fake Kaggle mount.

`tests/test_discovery.py` tests the helper in isolation. This file goes one step
further and imports `kernel/clipwright_worker.py` itself with the input/working
roots redirected at a temporary tree shaped like Kaggle's:

    <tmp>/working/                                  <- cwd, as on Kaggle
    <tmp>/input/clipwright-bridge-code/kaggle-bridge/processor/...
    <tmp>/input/clipwright-phase1-input/{job.json,sample.mp4}

If the worker could not find the package, the manifest or the video, this test
fails instead of a GPU session failing at hour one.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
KERNEL_PATH = BRIDGE_ROOT / "kernel" / "clipwright_worker.py"


def load_kernel() -> ModuleType:
    """Import kernel/clipwright_worker.py as a throwaway module."""
    spec = importlib.util.spec_from_file_location(
        "clipwright_worker_under_test", KERNEL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def kaggle_tree(tmp_path, monkeypatch) -> Path:
    """A Kaggle-shaped mount tree, with the environment pointed at it."""
    input_root = tmp_path / "input"
    working_root = tmp_path / "working"
    working_root.mkdir()

    code_dataset = input_root / "clipwright-bridge-code" / "kaggle-bridge"
    code_dataset.mkdir(parents=True)
    shutil.copytree(BRIDGE_ROOT / "processor", code_dataset / "processor")

    input_dataset = input_root / "clipwright-phase1-input"
    input_dataset.mkdir(parents=True)
    (input_dataset / "sample.mp4").write_bytes(b"\x00" * 2048)

    monkeypatch.setenv("CLIPWRIGHT_KAGGLE_INPUT", str(input_root))
    monkeypatch.setenv("CLIPWRIGHT_KAGGLE_WORKING", str(working_root))
    monkeypatch.delenv("BRIDGE_PATH", raising=False)
    monkeypatch.chdir(working_root)  # Kaggle starts the kernel in /kaggle/working
    return tmp_path


def write_job(input_root: Path, **source) -> Path:
    job = {
        "schema_version": "clipwright.kaggle.job/1",
        "job_id": "discovery-test",
        "source": {"type": "local_file", "filename": "sample.mp4", **source},
        "processing": {"mode": "gpu", "require_gpu": True},
    }
    path = input_root / "clipwright-phase1-input" / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def test_kernel_finds_the_package_in_the_code_dataset(kaggle_tree):
    kernel = load_kernel()
    expected = (kaggle_tree / "input" / "clipwright-bridge-code" / "kaggle-bridge").resolve()
    assert kernel.BRIDGE_ROOT == expected
    assert (kernel.BRIDGE_ROOT / "processor" / "__init__.py").is_file()


def test_kernel_finds_job_json_in_the_input_dataset(kaggle_tree):
    kernel = load_kernel()
    expected = write_job(kaggle_tree / "input")
    assert kernel.resolve_job_path(None) == expected


def test_kernel_prefers_a_job_placed_in_the_working_directory(kaggle_tree):
    kernel = load_kernel()
    input_root = kaggle_tree / "input"
    write_job(input_root)
    local = kaggle_tree / "working" / "job.json"
    local.write_text(json.dumps({"schema_version": "clipwright.kaggle.job/1"}), encoding="utf-8")
    assert kernel.resolve_job_path(None) == local


def test_kernel_finds_the_video_in_the_input_dataset(kaggle_tree):
    kernel = load_kernel()
    write_job(kaggle_tree / "input")
    job = json.loads((kaggle_tree / "input" / "clipwright-phase1-input" / "job.json").read_text())
    found = kernel.resolve_local_input(job)
    assert found.name == "sample.mp4"
    assert found.parent.name == "clipwright-phase1-input"


def test_kernel_errors_clearly_when_the_video_is_absent(kaggle_tree):
    kernel = load_kernel()
    (kaggle_tree / "input" / "clipwright-phase1-input" / "sample.mp4").unlink()
    job = {"source": {"type": "local_file", "filename": "missing.mp4"}}
    with pytest.raises(SystemExit) as error:
        kernel.resolve_local_input(job)
    assert "missing.mp4" in str(error.value)


def test_kernel_accepts_kaggle_renaming_a_single_video(kaggle_tree):
    """Kaggle sometimes keeps the archive's own filename; one video is unambiguous."""
    kernel = load_kernel()
    dataset = kaggle_tree / "input" / "clipwright-phase1-input"
    (dataset / "sample.mp4").rename(dataset / "kaggle-renamed-it.mp4")
    job = {"source": {"type": "local_file", "filename": "sample.mp4"}}
    assert kernel.resolve_local_input(job).name == "kaggle-renamed-it.mp4"


def test_kernel_errors_clearly_when_no_job_is_mounted(kaggle_tree):
    kernel = load_kernel()
    with pytest.raises(SystemExit) as error:
        kernel.resolve_job_path(None)
    message = str(error.value)
    assert "job.json" in message and "private dataset" in message


def test_kernel_surfaces_discovery_errors_as_system_exit(tmp_path, monkeypatch):
    """A missing package must become a readable SystemExit, not a traceback."""
    from processor import discovery

    def fail(**_kwargs):
        raise discovery.DiscoveryError("Could not find the 'processor' package. Searched: /x")

    monkeypatch.setattr(discovery, "find_bridge_root", fail)
    monkeypatch.setenv("CLIPWRIGHT_KAGGLE_INPUT", str(tmp_path / "empty-input"))
    monkeypatch.setenv("CLIPWRIGHT_KAGGLE_WORKING", str(tmp_path))
    monkeypatch.delenv("BRIDGE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        load_kernel()
    assert "'processor' package" in str(error.value)
