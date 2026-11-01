"""Dependency bootstrap: discovery, install plan, verification, redaction.

Nothing here imports torch, whisper or ffmpeg: `Probes` and the install `runner`
are injected, so the logic that decides "is the kernel ready?" is testable on a
machine that has none of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from processor import dependencies


def probes(
    *,
    torch: dict | None = None,
    whisper: dict | None = None,
    available: set[str] | None = None,
    versions: dict | None = None,
    binaries: dict | None = None,
) -> dependencies.Probes:
    """A Probes object with every heavy probe replaced by a stub."""
    torch_info = {"installed": True, "version": "2.4.0", "cuda_build": "12.4",
                  "cuda_available": True, "device_count": 2, "device_names": ["T4", "T4"]}
    if torch is not None:
        torch_info = torch
    whisper_info = {
        "importable": True,
        "has_load_model": True,
        "module_path": "/usr/lib/python3.11/site-packages/whisper/__init__.py",
        "distributions": ["openai-whisper"],
        "collision": False,
    }
    if whisper is not None:
        whisper_info = whisper
    known = {"torch", "whisper", "transformers", "accelerate", "sentencepiece", "yt_dlp"}
    available_modules = known if available is None else available
    known_versions = {
        "torch": "2.4.0",
        "openai-whisper": "20250625",
        "transformers": "4.46.0",
        "accelerate": "1.0.0",
        "sentencepiece": "0.2.0",
        "yt-dlp": "2025.9.5",
    }
    known_versions.update(versions or {})
    binary_report = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "ffprobe": "/usr/bin/ffprobe",
        "versions": {"ffmpeg": "ffmpeg version 6.1", "ffprobe": "ffprobe version 6.1"},
    }
    if binaries is not None:
        binary_report = binaries
    return dependencies.Probes(
        torch=lambda: torch_info,
        whisper=lambda: whisper_info,
        import_available=lambda name: name in available_modules,
        distribution=lambda name: known_versions.get(name),
        binaries=lambda: binary_report,
    )


def runner(*, code=0, out="", err=""):
    calls = []

    def run(command, timeout, env):
        calls.append((tuple(command), timeout, dict(env)))
        return code, out, err

    run.calls = calls
    return run


# --------------------------------------------------------------------------- #
# requirements parsing
# --------------------------------------------------------------------------- #


def test_parse_requirements_reads_the_shipped_file():
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    requirements = dependencies.parse_requirements(text)
    names = [requirement.canonical for requirement in requirements]
    assert "openai-whisper" in names
    assert "yt-dlp" in names
    assert "torch" not in names, "requirements.txt must never ask for torch"
    whisper = next(r for r in requirements if r.canonical == "openai-whisper")
    assert whisper.specifiers == (("==", "20250625"),)
    assert whisper.import_name == "whisper"


def test_parse_requirements_ignores_comments_blanks_and_flags():
    text = """
    # a comment
    # another

    -r other.txt
    --index-url https://example.invalid/simple
    yt-dlp>=2025.9.5   # inline comment
    """
    parsed = dependencies.parse_requirements(text)
    assert [requirement.canonical for requirement in parsed] == ["yt-dlp"]
    assert parsed[0].specifiers == ((">=", "2025.9.5"),)


def test_parse_requirements_skips_unparseable_specs():
    parsed = dependencies.parse_requirements("weird-package @ not-a-spec\n")
    assert parsed == []


@pytest.mark.parametrize(
    "version,spec,expected",
    [
        ("20250625", "==20250625", True),
        ("20250625", "==20240101", False),
        ("4.46.0", ">=4.46", True),
        ("4.45.9", ">=4.46", False),
        ("1.0.0", ">2.0", False),
        ("0.2.0", "~=0.2", True),
        ("0.3.0", "~=0.2", False),
        ("9.9.9", "", True),
    ],
)
def test_version_comparison(version, spec, expected):
    requirement = dependencies.parse_requirements(f"pkg{spec}\n")[0]
    assert requirement.version_ok(version) is expected


def test_version_ok_is_false_when_nothing_is_installed():
    requirement = dependencies.parse_requirements("yt-dlp>=2025.9.5")[0]
    assert requirement.version_ok(None) is False


def test_image_provided_packages_are_recognised():
    torch_req = dependencies.parse_requirements("torch==2.4.0")[0]
    assert torch_req.is_provided
    assert not dependencies.parse_requirements("transformers")[0].is_provided


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_find_requirements_in_the_code_dataset(tmp_path):
    bridge = tmp_path / "input" / "clipwright-bridge-code" / "kaggle-bridge"
    bridge.mkdir(parents=True)
    (bridge / "requirements.txt").write_text("yt-dlp\n", encoding="utf-8")
    found = dependencies.find_requirements(search_roots=[tmp_path / "input"])
    assert found == bridge / "requirements.txt"


def test_find_requirements_prefers_the_shallowest_copy(tmp_path):
    root = tmp_path / "input"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "requirements.txt").write_text("deep\n", encoding="utf-8")
    (root / "requirements.txt").write_text("shallow\n", encoding="utf-8")
    assert dependencies.find_requirements(search_roots=[root]) == root / "requirements.txt"


def test_find_requirements_returns_none_when_absent(tmp_path):
    assert dependencies.find_requirements(search_roots=[tmp_path]) is None


# --------------------------------------------------------------------------- #
# install planning
# --------------------------------------------------------------------------- #


def test_nothing_is_planned_when_everything_is_present():
    requirements = dependencies.parse_requirements("\n".join(dependencies.BUILTIN_SPECS))
    states = dependencies.inspect(requirements, probes())
    installable, refused = dependencies.install_plan(states)
    assert installable == [] and refused == []


def test_missing_packages_are_planned_and_present_ones_are_not():
    requirements = dependencies.parse_requirements("yt-dlp>=2025.9.5\ntransformers>=4.46\n")
    states = dependencies.inspect(requirements, probes(available={"yt_dlp"}))
    installable, refused = dependencies.install_plan(states)
    assert installable == ["transformers>=4.46"]


def test_missing_torch_is_refused_not_installed():
    requirements = dependencies.parse_requirements("torch\ntransformers")
    states = dependencies.inspect(requirements, probes(available=set()))
    installable, refused = dependencies.install_plan(states)
    assert refused == ["torch"]
    assert "transformers" in installable[0]


def test_pip_install_refuses_to_name_torch():
    with pytest.raises(dependencies.DependencyError) as error:
        dependencies.pip_install(["torch==2.4.0"], runner=runner())
    assert "CUDA" in str(error.value)


def test_pip_install_never_upgrades_satisfied_dependencies():
    recorder = runner(code=0, out="ok")
    dependencies.pip_install(["transformers>=4.46"], runner=recorder)
    command, timeout, env = recorder.calls[0]
    assert "--upgrade-strategy" in command and "only-if-needed" in command
    assert "--no-input" in command
    assert env["PIP_NO_INPUT"] == "1"
    assert timeout == dependencies.DEFAULT_INSTALL_TIMEOUT_SECONDS


def test_pip_install_reports_a_failure_with_a_redacted_tail():
    result = dependencies.pip_install(
        ["transformers>=4.46"],
        runner=runner(code=1, err="ERROR: could not find a version\nhttps://user:s3cr3t@index/simple"),
    )
    assert not result.ok and result.returncode == 1
    assert "s3cr3t" not in result.log_tail
    assert "could not find a version" in result.log_tail


def test_pip_install_handles_a_timeout():
    def boom(command, timeout, env):
        raise subprocess.TimeoutExpired(cmd="pip", timeout=timeout)

    result = dependencies.pip_install(["transformers"], runner=boom, timeout=5)
    assert not result.ok and "did not finish" in result.log_tail


def test_pip_install_handles_a_missing_python():
    def boom(command, timeout, env):
        raise FileNotFoundError()

    result = dependencies.pip_install(["transformers"], runner=boom)
    assert not result.ok and "not found" in result.log_tail


def test_dry_run_transcribes_the_command_without_running_it():
    recorder = runner()
    result = dependencies.pip_install(["transformers"], runner=recorder, dry_run=True)
    assert recorder.calls == []
    assert result.attempted is False
    assert "pip" in result.command


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def test_good_environment_passes_every_check():
    checks = dependencies.verify_environment(gpu_required=True, probes=probes())
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_cpu_torch_fails_the_cuda_build_check():
    cpu_torch = {"installed": True, "version": "2.4.0", "cuda_build": None,
                 "cuda_available": False, "device_count": 0}
    checks = {c.name: c for c in dependencies.verify_environment(gpu_required=True, probes=probes(torch=cpu_torch))}
    assert checks["torch_cuda_build"].ok is False
    assert checks["cuda_devices"].ok is False


def test_gpu_not_required_accepts_cpu_torch():
    cpu_torch = {"installed": True, "version": "2.4.0", "cuda_build": None,
                 "cuda_available": False, "device_count": 0}
    checks = {c.name: c for c in dependencies.verify_environment(gpu_required=False, probes=probes(torch=cpu_torch))}
    assert checks["torch"].ok is True
    assert "cuda_devices" not in checks


def test_legacy_whisper_distribution_is_caught():
    legacy = {
        "importable": True,
        "has_load_model": False,
        "module_path": "/site-packages/whisper/__init__.py",
        "distributions": ["whisper"],
        "collision": False,
    }
    checks = {c.name: c for c in dependencies.verify_environment(gpu_required=False, probes=probes(whisper=legacy))}
    assert checks["whisper_importable"].ok is False
    assert checks["whisper_is_openai_whisper"].ok is False


def test_two_whisper_providers_is_a_failure():
    collided = {
        "importable": True,
        "has_load_model": True,
        "module_path": "/site-packages/whisper/__init__.py",
        "distributions": ["openai-whisper", "whisper"],
        "collision": True,
    }
    checks = {c.name: c for c in dependencies.verify_environment(gpu_required=False, probes=probes(whisper=collided))}
    assert checks["whisper_single_provider"].ok is False


def test_missing_ffmpeg_and_ffprobe_are_reported_separately():
    checks = {
        c.name: c
        for c in dependencies.verify_environment(
            gpu_required=False, probes=probes(binaries={"ffmpeg": None, "ffprobe": "/usr/bin/ffprobe", "versions": {}})
        )
    }
    assert checks["binary_ffmpeg"].ok is False
    assert checks["binary_ffprobe"].ok is True


def test_missing_model_libraries_are_named():
    checks = {
        c.name: c
        for c in dependencies.verify_environment(
            gpu_required=False, probes=probes(available={"torch", "whisper"})
        )
    }
    assert checks["import_transformers"].ok is False
    assert checks["import_yt_dlp"].ok is False


# --------------------------------------------------------------------------- #
# bootstrap end-to-end (stubbed)
# --------------------------------------------------------------------------- #


def test_bootstrap_installs_only_missing_packages(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("yt-dlp>=2025.9.5\ntransformers>=4.46\n", encoding="utf-8")
    recorder = runner(code=0, out="Successfully installed transformers")
    report = dependencies.bootstrap(
        gpu_required=True,
        requirements_path=requirements,
        probes=probes(available={"torch", "whisper", "yt_dlp"}, versions={"transformers": None}),
        runner=recorder,
    )
    assert report.install.packages == ("transformers>=4.46",)
    command = recorder.calls[0][0]
    assert "yt-dlp>=2025.9.5" not in command
    assert str(requirements) == report.requirements_source


def test_bootstrap_reports_failure_when_install_fails(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("transformers>=4.46\n", encoding="utf-8")
    report = dependencies.bootstrap(
        gpu_required=False,
        requirements_path=requirements,
        probes=probes(available={"torch", "whisper"}, versions={"transformers": None}),
        runner=runner(code=1, err="ERROR: no matching distribution"),
    )
    assert not report.ok
    assert any("pip install failed" in problem for problem in report.problems)
    assert any("still unavailable" in problem for problem in report.problems)


def test_bootstrap_fails_when_torch_is_missing_instead_of_installing_it(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("torch\ntransformers>=4.46\n", encoding="utf-8")
    recorder = runner(code=0)
    report = dependencies.bootstrap(
        gpu_required=True,
        requirements_path=requirements,
        probes=probes(
            torch={"installed": False, "version": None, "cuda_build": None,
                   "cuda_available": False, "device_count": 0},
            available={"whisper", "transformers", "accelerate", "sentencepiece", "yt_dlp"},
            versions={"torch": None},
        ),
        runner=recorder,
    )
    assert not report.ok
    assert any("Refusing to pip-install" in problem for problem in report.problems)
    assert all("torch" not in package for call in recorder.calls for package in call[0])


def test_bootstrap_detects_cuda_disappearing_during_install(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("transformers>=4.46\n", encoding="utf-8")

    torch_states = iter(
        [
            {"installed": True, "version": "2.4.0", "cuda_build": "12.4", "cuda_available": True, "device_count": 2},
            {"installed": True, "version": "2.4.0", "cuda_build": None, "cuda_available": False, "device_count": 0},
        ]
    )
    base = probes(available={"torch", "whisper", "yt_dlp"}, versions={"transformers": None})
    base.torch = lambda: next(torch_states, {"installed": True, "version": "2.4.0", "cuda_build": None,
                                             "cuda_available": False, "device_count": 0})

    report = dependencies.bootstrap(
        gpu_required=True, requirements_path=requirements, probes=base, runner=runner(code=0)
    )
    assert not report.ok
    assert any("CUDA build disappeared" in problem for problem in report.problems)


def test_bootstrap_without_install_flag_reports_what_is_missing(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("transformers>=4.46\n", encoding="utf-8")
    report = dependencies.bootstrap(
        gpu_required=False,
        requirements_path=requirements,
        install=False,
        probes=probes(available={"torch", "whisper"}, versions={"transformers": None}),
        runner=runner(),
    )
    assert not report.ok
    assert any("installation is disabled" in problem for problem in report.problems)


def test_bootstrap_falls_back_to_builtin_specs(tmp_path):
    report = dependencies.bootstrap(
        gpu_required=True, search_roots=[tmp_path], probes=probes(), runner=runner()
    )
    assert report.requirements_source is None
    assert any("openai-whisper" in item for item in report.requested)
    assert report.ok


def test_bootstrap_uses_the_discovered_requirements_file(tmp_path):
    bridge = tmp_path / "input" / "code" / "kaggle-bridge"
    bridge.mkdir(parents=True)
    (bridge / "requirements.txt").write_text("yt-dlp>=2025.9.5\n", encoding="utf-8")
    report = dependencies.bootstrap(
        gpu_required=True, search_roots=[tmp_path / "input"], probes=probes(), runner=runner()
    )
    assert report.requirements_source == str(bridge / "requirements.txt")
    assert report.requested == ("yt-dlp>=2025.9.5",)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def test_report_is_json_serialisable_and_secret_free(tmp_path):
    import json

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("transformers>=4.46\n", encoding="utf-8")
    report = dependencies.bootstrap(
        gpu_required=False,
        requirements_path=requirements,
        probes=probes(available={"torch", "whisper"}, versions={"transformers": None}),
        runner=runner(code=1, err="ERROR https://user:sup3rs3cret@index/simple"),
    )
    payload = report.as_dict()
    text = json.dumps(payload)
    assert "sup3rs3cret" not in text
    assert payload["install"]["attempted"] is True

    path = dependencies.write_report(tmp_path / "dependencies.json", report)
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is False


def test_markdown_report_lists_packages_and_problems():
    report = dependencies.bootstrap(
        gpu_required=True,
        probes=probes(torch={"installed": True, "version": "2.4.0", "cuda_build": None,
                             "cuda_available": False, "device_count": 0}),
        runner=runner(),
    )
    markdown = report.to_markdown()
    assert "## Dependency bootstrap" in markdown
    assert "cuda_devices" in markdown
    assert "**Problems**" in markdown
    assert "sup3rs3cret" not in markdown
