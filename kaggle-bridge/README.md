# ClipWright Kaggle Processing Bridge — Phase 1 (prototype)

Runs one ClipWright video job on Kaggle's free **NvidiaTeslaT4** GPU and writes a
result manifest you can verify. It is **isolated**: nothing here imports the
production backend, and nothing in the production stack was changed to build it.

> **Phase 1 scope.** Prove that Kaggle can process real ClipWright videos: probe
> the hardware, transcribe with GPU Whisper, run the *production* clip-selection
> prompt against a local 3B model, render clips with the production encode
> settings, validate every output, and report honest numbers.
> No Oracle integration, no queue, no production changes. See
> `docs/PHASE2-ORACLE-DESIGN.md` for the design that follows this phase.

---

## What runs where

| Piece | Runs on | Notes |
|---|---|---|
| `kernel/clipwright_worker.py` | Kaggle GPU kernel | the whole Gate A→G pipeline |
| `processor/*` | Kaggle (and locally for tests) | pure Python + ffmpeg + torch; stdlib-only where possible |
| `scripts/*.ps1` | your Windows box | prepare, push, watch, fetch |
| `tests/*` | anywhere | no torch, no ffmpeg, no GPU needed |

---

## 1. Kaggle account and API setup (no credentials in this repo)

1. Create a Kaggle account, then **Settings → API → Create New Token**. That
   downloads `kaggle.json` — put it in `%USERPROFILE%\.kaggle\kaggle.json` and
   never copy it into this repository.
2. `pip install kaggle` (the official CLI). Verify with `kaggle --version`.
3. Phone-verify your Kaggle account. Free GPU kernels require it; without it the
   accelerator options are greyed out.

**Do not** paste API keys into job manifests, kernel metadata, scripts or logs.
The manifest validator actively rejects credential-shaped keys and values
(`bridge_token`, `hf_token`, `Bearer …`, signed URLs), so a leak fails loudly
instead of being uploaded.

For Phase 2, Kaggle's account-level **notebook secrets** are the right place for a
short-lived transfer token — see `docs/PHASE2-ORACLE-DESIGN.md` §4.

---

## 2. Two private datasets

Kaggle kernels only receive the code file plus attached datasets, so both the
bridge and the test video ship as **private** datasets.

```powershell
cd kaggle-bridge\scripts
.\prepare.ps1 -TestVideo "D:\clips\short-test.mp4"
```

Requirements: Python 3.9+ on `PATH` (the Kaggle CLI is a Python package, and the
packaging safeguards live in `processor/packaging.py` so they can be tested).

That produces:

* `dist/clipwright-bridge-code.zip` — upload as a **private** dataset `clipwright-bridge-code`
* `dist/clipwright-phase1-input.zip` — contains the video **and a `job.json` with
  its real size and SHA-256**; upload as a **private** dataset `clipwright-phase1-input`

How the archives are built:

* **ZIP64, not `Compress-Archive`.** `Compress-Archive` cannot store a file larger
  than 2 GB. Both archives are written by Python's `zipfile` module with ZIP64
  enabled (`processor/packaging.py`), so a large input is packaged correctly rather
  than silently truncated.
* **An explicit ceiling that fails before packaging.** The input video must be
  under `-MaxInputGb` (default **4 GB**); above that `prepare.ps1` stops before
  writing anything and tells you the actual size and how to raise the limit.
  Between 1 GB and the ceiling it warns: the bridge's own downloader refuses
  sources above 2 GiB, and Kaggle autosaves ~20 GB of output.
* **Caches are never shipped.** `__pycache__`, `.pytest_cache`, `.git` and
  `node_modules` are excluded, symlinks are refused, and each archive is re-opened
  and verified (entry list, CRC check, size, SHA-256) before the script moves on.
* **Metadata errors stop the script.** An invalid `kernel-metadata.json` is a
  failure, not a warning. `-AllowMetadataErrors` exists only for the first run,
  where you must build the datasets before you know their ids.

Run `python -m processor.packaging limits` to print the numbers the script uses.

Use a short, non-sensitive clip (60–120 s is plenty). Kaggle's current notebook
specifications allow **12 hours for CPU and GPU sessions** (TPU sessions are
capped at 9 hours), and the GPU quota is roughly 30 hours per week. A short clip
keeps every run well inside both limits, keeps the automatic commit of
`/kaggle/working` small, and makes the first failure cheap to repeat.

### Dataset privacy is separate from kernel privacy

`is_private: true` in `kernel-metadata.json` makes the **notebook** private. It does
not make the datasets it mounts private.

**Create both datasets as private, independently.** A public dataset's contents
(the test video, the bridge source) are downloadable by anyone with the link, even
when the only notebook that uses it is private, and a private kernel that fails can
leave its copied inputs visible in an output bundle. Kaggle's dataset visibility is
set per dataset in the dataset's Settings; the API does not expose it, so
`push.ps1` cannot check it for you and says so.

---

## 3. Configure and push

Fill in your username in `kernel/kernel-metadata.json`:

```json
{
  "id": "<your-kaggle-username>/clipwright-bridge-phase1",
  "dataset_sources": [
    "<your-kaggle-username>/clipwright-bridge-code",
    "<your-kaggle-username>/clipwright-phase1-input"
  ],
  "enable_gpu": true,
  "machine_shape": "NvidiaTeslaT4",
  "is_private": true,
  "enable_internet": true
}
```

`enable_internet` must stay `true` for Phase 1: the kernel `pip install`s
`openai-whisper` and `yt-dlp` at start because the dataset does not bundle wheels.
Nothing is uploaded; the internet is only used for package installation.

Then:

```powershell
.\push.ps1 -Wait     # pushes and polls until the run finishes
.\logs.ps1           # prints the run log
.\output.ps1         # downloads outputs and re-verifies every SHA-256
```

`push.ps1` refuses to push unless **all** of these hold, and prints what failed:

| Check | Enforced by |
|---|---|
| `is_private` is `true` | `processor.packaging check-metadata --strict` |
| `enable_gpu` is `true` and `machine_shape` is `NvidiaTeslaT4` | same |
| `enable_internet` is present and `true` | same |
| exactly two `dataset_sources`, distinct, `owner/slug` shaped | same |
| no placeholder usernames anywhere in the metadata | same |
| `clipwright_worker.py` exists next to the metadata | same |
| the Kaggle CLI is installed and its credentials are present | `processor.kaggle_cli describe` |
| those credentials **actually authenticate** | `processor.kaggle_cli verify-auth` |
| both configured datasets exist in the account | `processor.kaggle_cli verify-datasets` |

### Presence of `kaggle.json` is not authentication

A credentials file only proves a file exists. It can hold a revoked token, a
token for a different account, or a key with a stray newline — the failure then
appears minutes into a GPU run. `push.ps1` therefore makes a real authenticated API
call (`kaggle datasets list -m`) and fails with a specific reason:
`unauthenticated`, `missing-cli`, `timeout`, or `error`.

Credential values are never read, printed, or logged. The file's presence is
reported as *which keys exist* only, and every line of CLI output shown to you
passes through a redactor first, so a partially printed token cannot reach a
terminal, a log file, or a screenshot.

---

## 3a. How the kernel finds its inputs

Kaggle mounts each attached dataset at `/kaggle/input/<dataset-slug>/`, and the
mount name comes from the dataset slug — not from the zip's filename. Some uploads
keep the archive's top-level folder, others do not. Hard-coding a path works on
exactly one machine, once, so the kernel searches and then *verifies*:

| What | Where it looks | Verified by |
|---|---|---|
| the `processor` package | `/kaggle/input/<slug>/kaggle-bridge/`, `/kaggle/input/<slug>/`, one level deeper, `/kaggle/working`, cwd; `BRIDGE_PATH` overrides everything | the directory must contain `processor/__init__.py` |
| `job.json` | `/kaggle/working/job.json`, cwd, then `/kaggle/input/**/job.json` or `sample-job.json` | the JSON must parse **and** carry `clipwright.kaggle.job/1` |
| the video | the job's `source.path`, then an exact filename match anywhere under `/kaggle/input`, then a single video | if several videos are mounted and none matches the name, the run **fails and lists them** |

That last row matters: silently picking the first `.mp4` in a mount is how you
benchmark the wrong file. `source.size_bytes` (written by `prepare.ps1`) also
disambiguates, and a wrong pick is caught later by the SHA-256 check.

The search is bounded (depth 4), skips `__pycache__`, and does not follow
symlinks. All of it lives in `processor/discovery.py` and is tested against a fake
mount tree — including the failure messages — so it does not depend on a GPU
session to be exercised.

If discovery fails, the message names the paths it searched and what to attach.

Two environment variables exist **for tests only** — `CLIPWRIGHT_KAGGLE_INPUT` and
`CLIPWRIGHT_KAGGLE_WORKING` — so `tests/test_kernel_discovery.py` can import the
real kernel module against a temporary mount tree. They are never set on Kaggle,
where the defaults (`/kaggle/input`, `/kaggle/working`) are what the notebook sees.

---

## 4. What the kernel produces

Everything lands in `/kaggle/working/clipwright-output`:

| File | Contents |
|---|---|
| `environment.json` | Gate A: GPU, driver, CUDA, torch, Whisper module path, ffmpeg encoders, yt-dlp |
| `transcript.json` | ClipWright `version: 2` transcript cache — milliseconds, words + utterances |
| `ai-selection.json` | validated selection, rejected segments, prompt size, attempts |
| `clips/*.mp4` | rendered clips (1080×1920, libx264 CRF 19 preset medium, 30 fps, AAC 192k) |
| `captions/*.ass` | subtitle file when `rendering.captions` is true (plain reference styling) |
| `bench/` | the NVENC twin of clip 1, kept only with `--keep-benchmark-clips` |
| `benchmark.json` | machine-readable timings |
| `REPORT.md` | human-readable report, including "not measured" where that is the truth |
| `result.json` | Gate F result manifest with every artefact's size and SHA-256 |
| `job.json` | the validated job that actually ran |

Exit codes: `0` all gates passed · `2` GPU required but unavailable · `3` download
failed · `4` source invalid · `5` input missing/checksum mismatch · `6`
transcription failed · `7` partial (some gate failed) · `8` self-check failed.

---

## 5. The job manifest

See `config/sample-job.json`. Validation runs before anything else and reports
**every** problem at once.

```json
{
  "schema_version": "clipwright.kaggle.job/1",
  "job_id": "phase1-sample-001",
  "source": {"type": "local_file", "filename": "short-test.mp4", "sha256": "…"},
  "processing": {"mode": "gpu", "require_gpu": true},
  "transcription": {"enabled": true, "model": "base", "device": "auto"},
  "ai": {"enabled": true, "backend": "auto", "model": "llama3.2:3b"},
  "rendering": {"codec": "libx264", "crf": 19, "preset": "medium",
                "target": {"width": 1080, "height": 1920, "fps": 30}},
  "outputs": {"max_clips": 2}
}
```

`processing.require_gpu: true` is the important one: with CUDA unavailable the run
**fails** rather than quietly producing CPU numbers labelled as GPU benchmarks.

---

## 6. Running the tests

No GPU, torch, ffmpeg or Kaggle account required:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install pytest
cd kaggle-bridge
python -m pytest tests -q
```

197 tests (stdlib only) cover:

* **manifests** — schema versions, credential-shaped keys/values, path traversal,
  SHA-256 verification;
* **validation maths** — duration tolerance, fps, resolution, A/V sync, frame
  pacing, silence;
* **the selection contract** — JSON extraction from messy replies, defaults,
  coercions, rejection reasons, the production prompt text;
* **transcript conversion** — seconds→milliseconds, words, confidence defaults;
* **discovery** — the fake Kubernetes-style mount tree above, including the
  ambiguous-video failure;
* **packaging** — ZIP64 is requested, oversize inputs fail before anything is
  written, caches and symlinks are refused, archives are re-verified;
* **kernel metadata** — every blocker `push.ps1` enforces, with the shipped
  placeholder file asserted to be *not* pushable;
* **credential handling** — presence ≠ authentication, rejection reasons, and that
  a secret never appears in any message or JSON payload;
* **kernel wiring** — the kernel's AST is resolved against the real module
  signatures, so a renamed function fails the test instead of the GPU run.

## 7. Fidelity to production, and where it differs

Copied verbatim from `backend/src` at commit `a73607d`:

* the clip-selection system prompt and task prompt (`ai.py`);
* the transcript cache conversion, including millisecond timings and the
  `text`/`utterance`/`word` shape (`media/transcription.py`);
* the 16 kHz mono 64 kbps audio pre-extraction;
* the final encode arguments — `libx264`, `preset medium`, `crf 19`, `yuv420p`,
  `profile high`, `level 4.1`, `-r 30`, `keyint=120/min-keyint=30/scenecut=40`;
* the 1080×1920 export target and `scale … force_original_aspect_ratio=decrease` +
  pad framing.

Not ported in Phase 1 (all listed in `docs/COMPATIBILITY-NOTES.md`):

* windowed analysis + candidate rerank + transcript-line snapping (a short video
  fits one window, so those stages have nothing to do yet);
* ClipWright's caption templates (the bridge burns a plain reference ASS);
* the production YouTube PO-token/deno toolchain;
* media storage, ARQ queueing and the database — Phase 2.

---

## 8. Status of this prototype

Implemented and tested locally: manifests, validation, prompt/contract fidelity,
transcript conversion, input discovery, packaging + metadata/auth safeguards,
kernel orchestration, CLI scripts.

**Not yet executed on Kaggle** — that requires your account, your private
datasets, and a GPU session. Nothing in this README claims a GPU result. The first
real run produces `REPORT.md` with measured numbers; copy it into
`docs/BENCHMARK-REPORT.md` rather than trusting any estimate.

What *has* been executed, on a CPU-only development box:

* the full test suite above (`python -m pytest tests -q` → **197 passed**);
* the kernel's Gate A environment report, and its refusal to continue when a GPU
  is required and CUDA is absent (exit code 2);
* its input-resolution failure paths (missing video → exit 5 with a named file and
  the paths searched);
* a **mock Kaggle mount rehearsal**: the packaged code dataset extracted to
  `<fake>/input/clipwright-bridge-code/`, the kernel script alone in
  `<fake>/working/`, and `job.json` + a video in
  `<fake>/input/clipwright-phase1-input/`. Started from the working directory with
  no `BRIDGE_PATH`, the worker found the package in the dataset, found the real
  `job.json` (not the example shipped inside the code dataset — that shadowing was
  a real bug this rehearsal caught), resolved the video, and stopped at Gate G with
  `ffprobe is not installed` (exit 4), which is the correct answer on a box without
  ffmpeg;
* every `python -m processor.packaging ...` and `python -m processor.kaggle_cli ...`
  command line that `prepare.ps1` / `push.ps1` invoke, including the exit codes
  they branch on.

What cannot be executed here, and is therefore unverified: anything touching a
GPU, ffmpeg, `torch`, `openai-whisper`, Ollama, the Kaggle API, and the `.ps1`
files themselves (no PowerShell in this environment — which is precisely why their
logic lives in `processor/packaging.py` and `processor/kaggle_cli.py`).
