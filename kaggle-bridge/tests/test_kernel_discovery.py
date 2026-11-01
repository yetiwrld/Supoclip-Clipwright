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


def test_kernel_uses_a_lone_job_placed_in_the_working_directory(kaggle_tree):
    kernel = load_kernel()
    local = kaggle_tree / "working" / "job.json"
    local.write_text(json.dumps({"schema_version": "clipwright.kaggle.job/1"}), encoding="utf-8")
    assert kernel.resolve_job_path(None) == local


def test_kernel_refuses_to_guess_between_two_real_jobs(kaggle_tree):
    kernel = load_kernel()
    write_job(kaggle_tree / "input")
    local = kaggle_tree / "working" / "job.json"
    local.write_text(json.dumps({"schema_version": "clipwright.kaggle.job/1"}), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        kernel.resolve_job_path(None)
    assert "ambiguous" in str(error.value)


def test_kernel_honours_an_explicit_job_path(kaggle_tree):
    kernel = load_kernel()
    dataset_job = write_job(kaggle_tree / "input")
    chosen = kaggle_tree / "working" / "chosen.json"
    chosen.write_text(json.dumps({"schema_version": "clipwright.kaggle.job/1"}), encoding="utf-8")
    assert kernel.resolve_job_path(str(chosen)) == chosen
    assert kernel.resolve_job_path(str(dataset_job)) == dataset_job


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


# --------------------------------------------------------------------------- #
# dependency bootstrap wiring
# --------------------------------------------------------------------------- #


def _write_runnable_job(path: Path) -> Path:
    """A schema-valid job (based on the shipped sample) that stops before rendering."""
    payload = json.loads((BRIDGE_ROOT / "config" / "sample-job.json").read_text(encoding="utf-8"))
    payload["job_id"] = "dep-test"
    payload["source"]["filename"] = "sample.mp4"
    payload["processing"]["mode"] = "cpu"
    payload["processing"]["require_gpu"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_kernel_exits_9_when_dependencies_fail(kaggle_tree, monkeypatch, tmp_path):
    """A failed bootstrap must stop the run before anything expensive starts."""
    from processor import dependencies

    kernel = load_kernel()
    job_path = _write_runnable_job(kaggle_tree / "working" / "job.json")

    failing = dependencies.DependencyReport(problems=["cuda_devices: no GPU"], skipped=False)
    monkeypatch.setattr(dependencies, "bootstrap", lambda **kwargs: failing)
    monkeypatch.setattr(
        dependencies, "write_report", lambda path, report: Path(path).write_text("{}", encoding="utf-8")
    )

    output = tmp_path / "out"
    code = kernel.run(["--job", str(job_path), "--output", str(output)])
    assert code == 9
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "dependency bootstrap failed" in result["error"]["message"]
    assert (output / "dependencies.json").is_file()


def test_kernel_can_skip_the_bootstrap(kaggle_tree, monkeypatch, tmp_path):
    """--skip-dependency-bootstrap must not touch pip or the probes."""
    from processor import dependencies

    kernel = load_kernel()
    job_path = _write_runnable_job(kaggle_tree / "working" / "job.json")

    def explode(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("bootstrap should be skipped")

    monkeypatch.setattr(dependencies, "bootstrap", explode)
    output = tmp_path / "out-skip"
    code = kernel.run(
        ["--job", str(job_path), "--output", str(output), "--skip-dependency-bootstrap"]
    )
    # With no ffmpeg/ffprobe in this environment the run stops at source validation.
    assert code in (4, 5)
    assert not (output / "dependencies.json").exists()


def test_two_runs_in_one_working_directory_do_not_conflict(kaggle_tree, tmp_path):
    """The kernel's own output/job.json must not become a rival manifest."""
    kernel = load_kernel()
    job_path = _write_runnable_job(kaggle_tree / "input" / "clipwright-phase1-input" / "job.json")
    output = tmp_path / "out-twice"
    first = kernel.run(
        ["--job", str(job_path), "--output", str(output), "--skip-dependency-bootstrap"]
    )
    assert (output / "job.json").is_file()
    # Second run with auto-detection: the dataset job is the only real manifest.
    second_manifest = kernel.resolve_job_path(None)
    assert second_manifest == job_path
    assert first in (4, 5, 6, 7, 8)
