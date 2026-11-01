"""ClipWright Kaggle processing bridge (Phase 1 prototype).

Independent of the production backend on purpose: nothing in this package
imports ``backend.src``. The *contracts* are shared (transcript cache schema,
clip-selection prompts, render settings), and they are copied verbatim with
provenance notes so drift is visible in review rather than at runtime.

Submodules are imported explicitly by the kernel and the tests so that this
package's import stays cheap and side-effect free.

``dependencies``, ``discovery``, ``packaging`` and ``kaggle_cli`` are stdlib-only
helpers used by the kernel bootstrap and by the PowerShell scripts in ``scripts/``;
they hold the logic that would otherwise be untestable inside a ``.ps1`` file.
"""

__all__ = [
    "ai_inference",
    "dependencies",
    "discovery",
    "downloader",
    "environment",
    "kaggle_cli",
    "manifest",
    "packaging",
    "rendering",
    "reporting",
    "transcription",
    "validation",
]

BRIDGE_PHASE = 1
