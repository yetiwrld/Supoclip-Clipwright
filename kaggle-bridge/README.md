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

That produces:

* `dist/clipwright-bridge-code.zip` — upload as a private dataset `clipwright-bridge-code`
* `dist/clipwright-phase1-input.zip` — contains the video **and a `job.json` with
  its real size and SHA-256**; upload as private dataset `clipwright-phase1-input`

Use a short, non-sensitive clip (60–120 s is plenty) so the run stays inside the
9-hour GPU session limit and the output stays small.

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
  "is_private": true
}
```

Then:

```powershell
.\push.ps1 -Wait     # pushes and polls until the run finishes
.\logs.ps1           # prints the run log
.\output.ps1         # downloads outputs and re-verifies every SHA-256
```

`push.ps1` refuses to run while placeholders remain, the code file is missing, or
the kernel is not private.

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

70 tests cover the job/result manifest contract (including credential rejection),
duration/fps/resolution/AV-sync/frame-pacing validation, JSON extraction from
messy model replies, the clip-selection contract (defaults, coercions, rejection
reasons), and the transcript-cache conversion.

---

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
transcript conversion, kernel orchestration, CLI scripts.

**Not yet executed on Kaggle** — that requires your account, your private
datasets, and a GPU session. Nothing in this README claims a GPU result. The first
real run produces `REPORT.md` with measured numbers; copy it into
`docs/BENCHMARK-REPORT.md` rather than trusting any estimate.
