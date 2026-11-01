"""Credential handling: presence is not authentication, and secrets never leak."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from processor import kaggle_cli

SECRET = "0123456789abcdef0123456789abcdef"


def runner_returning(code: int, out: str = "", err: str = ""):
    calls = []

    def runner(command, timeout):
        calls.append((tuple(command), timeout))
        return code, out, err

    runner.calls = calls
    return runner


def raising_runner(error: Exception):
    def runner(command, timeout):
        raise error

    return runner


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        f'{{"username": "someone", "key": "{SECRET}"}}',
        f"KAGGLE_KEY={SECRET}",
        f"X-Api-Key: {SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"token: {SECRET}",
    ],
)
def test_redact_removes_credential_values(raw):
    cleaned = kaggle_cli.redact(raw)
    assert SECRET not in cleaned
    assert "<redacted>" in cleaned


def test_redact_keeps_the_username_but_drops_the_key():
    cleaned = kaggle_cli.redact(f'{{"username": "alice", "key": "{SECRET}"}}')
    assert "alice" in cleaned
    assert SECRET not in cleaned


def test_redact_tolerates_empty_input():
    assert kaggle_cli.redact("") == ""


# --------------------------------------------------------------------------- #
# presence vs. authentication
# --------------------------------------------------------------------------- #


def test_describe_config_reports_keys_without_reading_values(tmp_path):
    path = tmp_path / "kaggle.json"
    path.write_text(json.dumps({"username": "alice", "key": SECRET}), encoding="utf-8")
    info = kaggle_cli.describe_config(path)
    assert info["exists"] and info["readable_json"]
    assert info["username_present"] and info["key_present"]
    assert SECRET not in json.dumps(info)


def test_describe_config_flags_a_missing_key(tmp_path):
    path = tmp_path / "kaggle.json"
    path.write_text(json.dumps({"username": "alice"}), encoding="utf-8")
    info = kaggle_cli.describe_config(path)
    assert info["exists"] and info["username_present"] and not info["key_present"]


def test_describe_config_handles_a_missing_file(tmp_path):
    info = kaggle_cli.describe_config(tmp_path / "absent.json")
    assert info["exists"] is False


def test_describe_config_handles_malformed_json(tmp_path):
    path = tmp_path / "kaggle.json"
    path.write_text("{oops", encoding="utf-8")
    info = kaggle_cli.describe_config(path)
    assert info["exists"] and not info["readable_json"]


def test_presence_alone_does_not_count_as_authenticated(tmp_path):
    """A complete kaggle.json plus a rejected API call must still fail."""
    path = tmp_path / "kaggle.json"
    path.write_text(json.dumps({"username": "alice", "key": SECRET}), encoding="utf-8")
    assert kaggle_cli.describe_config(path)["key_present"] is True

    check = kaggle_cli.verify_authentication(
        runner=runner_returning(1, out="", err="401 - Unauthorized")
    )
    assert check.ok is False
    assert check.reason == "unauthenticated"


def test_successful_probe_is_the_only_success_path():
    check = kaggle_cli.verify_authentication(
        runner=runner_returning(0, out="ref,title\nsomeone/code,Code\n")
    )
    assert check.ok and check.reason == ""
    assert "authenticated" in check.detail


def test_rejected_credentials_produce_an_actionable_hint():
    check = kaggle_cli.verify_authentication(
        runner=runner_returning(1, err="403 - Forbidden")
    )
    assert not check.ok
    assert "Create New Token" in check.detail
    assert SECRET not in check.detail


def test_failed_probe_output_is_redacted():
    check = kaggle_cli.verify_authentication(
        runner=runner_returning(1, err=f'{{"key": "{SECRET}"}} 500 server error')
    )
    assert SECRET not in check.detail
    assert SECRET not in json.dumps(check.as_dict())


def test_missing_cli_is_reported_without_a_traceback():
    check = kaggle_cli.verify_authentication(runner=raising_runner(FileNotFoundError()))
    assert not check.ok and check.reason == "missing-cli"
    assert "pip install kaggle" in check.detail


def test_timeout_is_reported():
    check = kaggle_cli.verify_authentication(
        runner=raising_runner(subprocess.TimeoutExpired(cmd="kaggle", timeout=90))
    )
    assert not check.ok and check.reason == "timeout"


def test_oserror_is_reported():
    check = kaggle_cli.verify_authentication(runner=raising_runner(OSError("permission denied")))
    assert not check.ok and check.reason == "missing-cli"


def test_the_probe_is_an_authenticated_call_not_a_config_read():
    runner = runner_returning(0, out="ref,title\n")
    kaggle_cli.verify_authentication(runner=runner)
    command, timeout = runner.calls[0]
    assert command[:2] == ("kaggle", "datasets")
    assert "list" in command and "-m" in command
    assert timeout == kaggle_cli.DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# dataset presence
# --------------------------------------------------------------------------- #


def test_parse_dataset_csv_skips_headers_blanks_and_noise():
    output = (
        "ref,title,size,lastUpdated\n"
        "someone/clipwright-bridge-code,Code,1MB,2026-01-01\n"
        "\n"
        '"someone/clipwright-phase1-input",Input,2MB,2026-01-02\n'
        "not a ref\n"
    )
    assert kaggle_cli.parse_dataset_csv(output) == [
        "someone/clipwright-bridge-code",
        "someone/clipwright-phase1-input",
    ]


def test_verify_datasets_accepts_case_insensitive_matches():
    check = kaggle_cli.verify_datasets(
        ["someone/Code", "someone/Input"],
        runner=runner_returning(0, out="ref,title\nsomeone/code,x\nsomeone/input,y\n"),
    )
    assert check.ok, check.detail


def test_verify_datasets_names_every_missing_dataset():
    check = kaggle_cli.verify_datasets(
        ["someone/code", "someone/absent"],
        runner=runner_returning(0, out="ref,title\nsomeone/code,x\n"),
    )
    assert not check.ok and check.reason == "missing-datasets"
    assert "someone/absent" in check.detail
    assert "PRIVATE" in check.detail


def test_verify_datasets_fails_when_listing_fails():
    check = kaggle_cli.verify_datasets(
        ["someone/code"], runner=runner_returning(1, err="403 Forbidden")
    )
    assert not check.ok and check.reason == "error"


def test_list_own_datasets_survives_a_missing_cli():
    ok, refs, detail = kaggle_cli.list_own_datasets(runner=raising_runner(FileNotFoundError()))
    assert not ok and refs == []
    assert "pip install kaggle" in detail


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_describe_exit_codes(tmp_path, monkeypatch, capsys):
    config = tmp_path / "kaggle.json"
    config.write_text(json.dumps({"username": "alice", "key": SECRET}), encoding="utf-8")
    monkeypatch.setenv("KAGGLE_CONFIG_DIR", str(tmp_path))
    assert kaggle_cli.main(["describe"]) == 0
    assert SECRET not in capsys.readouterr().out

    config.write_text(json.dumps({"username": "alice"}), encoding="utf-8")
    assert kaggle_cli.main(["describe"]) == 1

    config.unlink()
    assert kaggle_cli.main(["describe"]) == 1


def test_cli_verify_datasets_requires_at_least_one_ref(capsys):
    with pytest.raises(SystemExit):
        kaggle_cli.main(["verify-datasets"])
