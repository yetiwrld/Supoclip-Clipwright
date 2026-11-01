"""Static wiring check: everything the kernel calls must exist on its module.

The kernel cannot execute here (no ffmpeg, no GPU), so this walks its AST and
asserts every ``module.attribute`` reference resolves, and that the keyword
arguments used match the real signatures. That catches the class of bug a
compiled-but-never-run kernel is most likely to contain.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import processor
from processor import ai_inference, dependencies, downloader, environment, manifest
from processor import rendering, reporting
from processor import transcription, validation

KERNEL = Path(__file__).resolve().parents[1] / "kernel" / "clipwright_worker.py"

MODULES = {
    "dependencies": dependencies,
    "manifest": manifest,
    "validation": validation,
    "ai_inference": ai_inference,
    "transcription": transcription,
    "rendering": rendering,
    "environment": environment,
    "downloader": downloader,
    "reporting": reporting,
}


def _kernel_tree() -> ast.Module:
    return ast.parse(KERNEL.read_text(encoding="utf-8"))


def _imported_module_aliases(tree: ast.AST) -> set[str]:
    """Names bound to ``processor`` submodules by the kernel's import block."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "processor":
            aliases.update(alias.asname or alias.name for alias in node.names)
    return aliases


def _processor_attributes(tree: ast.AST):
    """Yield ``(module_name, attribute, node)`` for bridge-module attribute access.

    Catches both spellings the kernel uses: ``processor.manifest.validate_job`` and
    ``manifest.validate_job`` after ``from processor import manifest``.
    """
    aliases = _imported_module_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        base = node.value.id
        if base == "processor":
            continue  # handled through the aliases below
        if base in aliases and base in MODULES:
            yield base, node.attr, node


def _qualified_processor_attributes(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            if base.value.id == "processor" and base.attr in MODULES:
                yield base.attr, node.attr, node


def test_the_wiring_scan_actually_finds_references():
    """Guards against a vacuous test: the kernel must reference bridge modules."""
    found = list(_processor_attributes(_kernel_tree())) + list(
        _qualified_processor_attributes(_kernel_tree())
    )
    assert len(found) >= 20, f"only found {len(found)} module references"


def test_every_referenced_processor_attribute_exists():
    missing = [
        f"processor.{module}.{attribute}"
        for module, attribute, _ in list(_processor_attributes(_kernel_tree()))
        + list(_qualified_processor_attributes(_kernel_tree()))
        if not hasattr(MODULES[module], attribute)
    ]
    assert missing == []


def test_keyword_arguments_match_the_real_signatures():
    tree = _kernel_tree()
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    problems = []
    for module, attribute, node in list(_processor_attributes(tree)) + list(
        _qualified_processor_attributes(tree)
    ):
        target = getattr(MODULES[module], attribute)
        if not callable(target):
            continue
        for call in calls:
            if call.func is not node:
                continue
            keywords = {kw.arg for kw in call.keywords if kw.arg}
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):  # pragma: no cover - builtins only
                continue
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                continue
            unexpected = keywords - set(signature.parameters)
            if unexpected:
                problems.append(
                    f"processor.{module}.{attribute}: unexpected kwargs {sorted(unexpected)}"
                )
    assert problems == []


def _exit_codes() -> set[int]:
    """Every integer literal the kernel can return or exit with."""
    codes: set[int] = set()
    for node in ast.walk(_kernel_tree()):
        if isinstance(node, ast.Return) and node.value is not None:
            candidates = [node.value]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "SystemExit":
            candidates = list(node.args)
        else:
            continue
        for candidate in candidates:
            # Handles `return 2` and `return 0 if ok else 7` without picking up
            # integers that merely appear inside an f-string or an expression.
            if isinstance(candidate, ast.IfExp):
                candidates_inner = [candidate.body, candidate.orelse]
            else:
                candidates_inner = [candidate]
            for inner in candidates_inner:
                if isinstance(inner, ast.Constant) and isinstance(inner.value, int):
                    codes.add(inner.value)
    return codes


def test_kernel_defines_the_documented_exit_codes():
    assert _exit_codes() == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}


def test_kernel_never_imports_the_production_backend():
    tree = _kernel_tree()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"backend", "src", "api", "workers", "services"}


def test_kernel_has_no_credential_literals():
    source = KERNEL.read_text(encoding="utf-8").lower()
    for needle in ("kaggle.json", "api_key=", "api-key", "bearer "):
        assert needle not in source
