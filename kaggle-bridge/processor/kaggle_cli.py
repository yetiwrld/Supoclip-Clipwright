"""Prove the Kaggle CLI is *authenticated*, not merely installed.

``~/.kaggle/kaggle.json`` existing proves that a file exists. It does not prove
the credentials are valid, that the account is phone-verified (which free GPU
notebooks require), or that the token was ever accepted by Kaggle. The only honest
check is an authenticated API call, so that is what :func:`verify_authentication`
does — and it classifies the failure instead of dumping the CLI's stderr, which
can contain a partially printed token.

Credential handling: this module never reads the *values* in ``kaggle.json``. It
only checks which keys are present, and every string it emits passes through
:func:`redact` first.

Run as::

    python -m processor.kaggle_cli describe          # presence only, no secrets
    python -m processor.kaggle_cli verify-auth       # real API call
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

#: A cheap authenticated endpoint. ``datasets list -m`` requires credentials and
#: returns at most a page of the caller's own datasets.
AUTH_PROBE_ARGS: Tuple[str, ...] = ("datasets", "list", "-m")
DEFAULT_TIMEOUT_SECONDS = 90

Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]

#: (pattern, replacement) pairs applied in order by :func:`redact`.
_SECRET_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    # Credentials embedded in an index URL, with or without a password:
    # https://user:token@host/simple and https://token@host/simple
    (re.compile(r"(?i)(\bhttps?://)[^/\s@]+@"), r"\1<redacted>@"),
    # JSON (``"key": "abc"``), .env (``KEY=abc``) and header (``Key: abc``) forms.
    (
        re.compile(r"(?i)(\"?(?:key|token|secret|password)\"?\s*[:=]\s*)\"?[^\s,;\"'}]+"),
        r"\1<redacted>",
    ),
    # Any remaining opaque token of 32+ characters.
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<redacted>"),
)


class KaggleAuthError(Exception):
    """Raised when authentication cannot even be attempted."""


@dataclass(frozen=True)
class AuthCheck:
    ok: bool
    detail: str
    command: Tuple[str, ...] = ()
    reason: str = ""  # "ok" | "unauthenticated" | "missing-cli" | "timeout" | "error"

    def as_dict(self) -> Dict[str, str]:
        return {
            "status": "ok" if self.ok else "fail",
            "reason": self.reason,
            "detail": self.detail,
            "command": " ".join(self.command),
        }


def redact(text: str) -> str:
    """Strip anything that looks like a credential from ``text``.

    Covers JSON/``.env``/header forms, 32+ character opaque tokens, and the
    ``https://user:token@index`` URLs pip prints when it talks to an authenticated
    package index. Every pattern is applied, in order.
    """
    if not text:
        return ""
    cleaned = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def config_dir() -> Path:
    override = os.environ.get("KAGGLE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".kaggle"


def config_path() -> Path:
    return config_dir() / "kaggle.json"


def describe_config(path: Optional[Path] = None) -> Dict[str, object]:
    """Report which credential keys exist. Never returns or prints their values."""
    path = Path(path) if path else config_path()
    info: Dict[str, object] = {
        "path": str(path),
        "exists": path.is_file(),
        "username_present": False,
        "key_present": False,
        "readable_json": False,
    }
    if not info["exists"]:
        return info
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return info
    if isinstance(payload, dict):
        info["readable_json"] = True
        info["username_present"] = bool(str(payload.get("username") or "").strip())
        info["key_present"] = bool(str(payload.get("key") or "").strip())
    return info


def _default_runner(command: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _classify(output: str) -> str:
    lowered = output.lower()
    if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "invalid api", "credentials")):
        return "unauthenticated"
    if "command not found" in lowered or "is not recognized" in lowered:
        return "missing-cli"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return "error"


def verify_authentication(
    *,
    cli: str = "kaggle",
    runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    probe_args: Sequence[str] = AUTH_PROBE_ARGS,
) -> AuthCheck:
    """Run an authenticated API call and report what actually happened."""
    command = (cli, *probe_args)
    run = runner or _default_runner
    try:
        returncode, stdout, stderr = run(command, timeout)
    except FileNotFoundError:
        return AuthCheck(
            ok=False,
            detail=f"the '{cli}' CLI is not installed (pip install kaggle)",
            command=command,
            reason="missing-cli",
        )
    except subprocess.TimeoutExpired:
        return AuthCheck(
            ok=False,
            detail=f"no response from Kaggle within {timeout}s; check connectivity and retry",
            command=command,
            reason="timeout",
        )
    except OSError as error:
        return AuthCheck(
            ok=False,
            detail=redact(f"could not run the Kaggle CLI: {error}"),
            command=command,
            reason="missing-cli",
        )

    if returncode == 0:
        return AuthCheck(
            ok=True,
            detail="authenticated (datasets list -m succeeded)",
            command=command,
        )

    combined = redact(f"{stderr}\n{stdout}").strip()
    reason = _classify(combined)
    hints = {
        "unauthenticated": (
            "Kaggle rejected the credentials. Create a fresh API token in the Kaggle UI "
            "(Account -> Settings -> API -> Create New Token) and replace "
            "~/.kaggle/kaggle.json."
        ),
        "missing-cli": "the Kaggle CLI is not installed (pip install kaggle)",
        "timeout": "the Kaggle API did not answer in time; check connectivity and retry",
        "error": "the authenticated probe failed; see the redacted output below",
    }
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "")
    return AuthCheck(
        ok=False,
        detail=f"{hints[reason]} — {first_line[:200]}".strip(),
        command=command,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# dataset presence
# --------------------------------------------------------------------------- #


def parse_dataset_csv(output: str) -> List[str]:
    """Extract ``owner/dataset-slug`` references from ``kaggle datasets list --csv``."""
    refs: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("ref,"):
            continue
        candidate = line.split(",")[0].strip().strip('"')
        if "/" in candidate and " " not in candidate:
            refs.append(candidate)
    return refs


def list_own_datasets(
    *,
    cli: str = "kaggle",
    runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bool, List[str], str]:
    """Return (ok, refs, detail) for the caller's own datasets."""
    command = (cli, "datasets", "list", "-m", "--csv")
    run = runner or _default_runner
    try:
        returncode, stdout, stderr = run(command, timeout)
    except FileNotFoundError:
        return False, [], f"the '{cli}' CLI is not installed (pip install kaggle)"
    except subprocess.TimeoutExpired:
        return False, [], f"no response from Kaggle within {timeout}s"
    except OSError as error:  # pragma: no cover - environment specific
        return False, [], redact(str(error))

    if returncode != 0:
        combined = redact(f"{stderr}\n{stdout}").strip()
        first = next((line.strip() for line in combined.splitlines() if line.strip()), "")
        return False, [], f"listing datasets failed: {first[:200]}"

    refs = parse_dataset_csv(stdout)
    return True, refs, f"{len(refs)} dataset(s) visible to this account"


def verify_datasets(
    refs: Sequence[str],
    *,
    cli: str = "kaggle",
    runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> AuthCheck:
    """Confirm every configured dataset exists in the account before pushing.

    Privacy cannot be read from this listing; the README requires both datasets to
    be created private, because dataset visibility is independent of kernel
    privacy. This check catches typos and never-created datasets, which otherwise
    surface as a confusing kernel error minutes into a run.
    """
    ok, available, detail = list_own_datasets(cli=cli, runner=runner, timeout=timeout)
    if not ok:
        return AuthCheck(
            ok=False,
            detail=detail,
            command=(cli, "datasets", "list", "-m", "--csv"),
            reason="error",
        )

    wanted = [str(ref).strip() for ref in refs]
    lowered = {ref.lower() for ref in available}
    missing = [ref for ref in wanted if ref.lower() not in lowered]
    if missing:
        return AuthCheck(
            ok=False,
            detail=(
                "dataset(s) not found in this account: "
                + ", ".join(missing)
                + ". Create them as PRIVATE datasets (dataset privacy is separate from "
                "kernel privacy) or fix dataset_sources."
            ),
            command=(cli, "datasets", "list", "-m", "--csv"),
            reason="missing-datasets",
        )
    return AuthCheck(
        ok=True,
        detail=f"all {len(wanted)} dataset(s) present: " + ", ".join(wanted),
        command=(cli, "datasets", "list", "-m", "--csv"),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m processor.kaggle_cli")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("describe", help="report credential file presence (no secrets)")
    verify = sub.add_parser("verify-auth", help="run an authenticated API call")
    verify.add_argument("--cli", default="kaggle")
    verify.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    datasets = sub.add_parser(
        "verify-datasets", help="confirm every configured dataset exists in the account"
    )
    datasets.add_argument("--ref", action="append", required=True)
    datasets.add_argument("--cli", default="kaggle")
    datasets.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    if args.command == "describe":
        info = describe_config()
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            state = "present" if info["exists"] else "MISSING"
            print(f"  {state}  {info['path']}")
            if info["exists"]:
                print(f"       readable json: {info['readable_json']}")
                print(f"       username key:  {info['username_present']}")
                print(f"       api key:       {'present (not shown)' if info['key_present'] else 'MISSING'}")
            else:
                print("       create one: Kaggle UI -> Account -> Settings -> API -> Create New Token")
        return 0 if info["exists"] and info["username_present"] and info["key_present"] else 1

    if args.command == "verify-auth":
        check = verify_authentication(cli=args.cli, timeout=args.timeout)
        if args.json:
            print(json.dumps(check.as_dict(), indent=2))
        else:
            print(("  ok   " if check.ok else "  FAIL ") + check.detail)
            print(f"       probe: {' '.join(check.command)}")
        return 0 if check.ok else 1

    if args.command == "verify-datasets":
        check = verify_datasets(args.ref, cli=args.cli, timeout=args.timeout)
        if args.json:
            print(json.dumps(check.as_dict(), indent=2))
        else:
            print(("  ok   " if check.ok else "  FAIL ") + check.detail)
        return 0 if check.ok else 1

    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
