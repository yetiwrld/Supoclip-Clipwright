# Kaggle bridge benchmark report

**Status: no GPU measurement exists yet.**

This file is the report *shell* produced by Phase 1. Every table below is filled
from `benchmark.json` after a real kernel run — by hand, from the artefact, not from
an estimate. Until a run has happened, the honest answer to "how fast is it?" is
**unknown**, and this document says so rather than guessing.

Kit: Kaggle `NvidiaTeslaT4` (2 × T4, 16 GB each), 4 vCPU, 16 GB RAM. Current Kaggle
notebook specifications allow **12 h for CPU and GPU sessions** and 9 h for TPU
sessions; ~20 GB of `/kaggle/working` is autosaved.

---

## How a run fills this in

```powershell
cd kaggle-bridge\scripts
.\prepare.ps1 -TestVideo "D:\clips\short-test.mp4"
.\push.ps1 -Wait
.\output.ps1                 # downloads to ..\runs\<timestamp>, re-verifies SHAs
```

Then copy the numbers out of `benchmark.json` (or `REPORT.md`) into the tables.

---

## 1. Environment (Gate A)

| Item | Value | Source |
|---|---|---|
| GPU | _not measured_ | `environment.json → gpu.devices` |
| Driver / CUDA | _not measured_ | same |
| PyTorch (version, CUDA build, cuDNN) | _not measured_ | `environment.json → python.torch` |
| Whisper module (path, provider) | _not measured_ | `environment.json → python.whisper` |
| ffmpeg (version, encoder list) | _not measured* | `environment.json → ffmpeg` |
| NVENC usable (1 s test encode) | _not measured_ | `environment.json → ffmpeg.nvenc` |
| Dependencies installed this run | _not measured_ | `dependencies.json → install` |
| Bootstrap wall time | _not measured_ | `benchmark.json → phases[gate_a0_dependencies]` |
| Peak GPU memory | _not measured_ | `benchmark.json → phases[].peak_gpu_memory_mb` |

\* The *only* environment probe that has executed so far is the local sandbox one,
where ffmpeg and torch are absent by design — that run correctly reported "ffmpeg is
not installed or not on PATH" and aborted with exit code 2. It is not a GPU result.

## 2. Transcription

| Metric | Definition | Value |
|---|---|---|
| Model | `tiny` / `base` / `small` (`openai-whisper`) | _not measured_ |
| Audio length | seconds of extracted 16 kHz mono | _not measured_ |
| Wall time | `transcribe()` duration | _not measured_ |
| RTF | wall time ÷ audio length | _not measured_ |
| Words / utterances | cache contents | _not measured_ |
| Word timestamps present | true/false | _not measured_ |

## 3. Clip selection (3B model)

| Metric | Value |
|---|---|
| Backend chosen (`ollama` or `transformers`) and model id | _not measured_ |
| Prompt characters | _not measured_ |
| Attempts (1 = valid first time) | _not measured_ |
| Wall time | _not measured_ |
| Segments returned / accepted / rejected | _not measured_ |
| Rejection reasons | _not measured_ |

## 4. Rendering — CPU (`libx264`) vs NVENC

Both must be measured **separately**; a single "render time" hides the interesting
number. Production default stays `libx264` (CRF 19, preset medium) regardless of what
this table says — this is a measurement, not a config change.

| | `libx264` (production default) | `h264_nvenc` (benchmark twin) |
|---|---|---|
| Clip length | _not measured_ | _not measured_ |
| Wall time | _not measured_ | _not measured_ |
| Speed factor (clip ÷ wall) | _not measured_ | _not measured_ |
| Output size | _not measured_ | _not measured_ |
| Bitrate | _not measured_ | _not measured_ |
| Quality note | reference | visual comparison needed before any claim |

Speed is only reported when the benchmark twin actually rendered. If NVENC was
unavailable, the cell reads **not available** — never a CPU time relabelled as GPU.

## 5. Validation (Gate G)

| Clip | Duration | FPS | Resolution | A/V sync | Frame pacing | Silence | Result |
|---|---|---|---|---|---|---|---|
| clip-01 | _not measured_ | | | | | | |
| clip-02 | _not measured_ | | | | | | |

## 6. Comparison against local hardware

There is **no local baseline in `sample-job.json`** (`baseline_local.status` is
`"not measured"`), so this run cannot make a "N× faster" claim. Once a matched local
run exists on the Ryzen 5 4600U box (same source, same Whisper model, same render
settings), paste its numbers here and the ratio becomes defensible:

| | Kaggle T4 | Local (R5 4600U, CPU) | Ratio |
|---|---|---|---|
| Whisper `base` | — | — | — |
| Whisper `small` | — | — | — |
| Render 1 clip | — | — | — |

## 7. Quota and wall-clock budget

| Run | GPU minutes used | Session wall time | Notes |
|---|---|---|---|
| _not measured_ | | | |

Kaggle free GPU quota is ~30 h/week. At the per-clip numbers above, state how many
clips fit in a week, and state what happens at exhaustion (kernel refuses the GPU
rather than silently running on CPU).

---

## Known limits of this measurement (fill in honestly after the first run)

* One short clip ≠ a 90-minute podcast; the pipeline currently analyses one window
  (see `docs/COMPATIBILITY-NOTES.md` §6), so long-source throughput is **not**
  measured by this run.
* NVENC quality is not "equal" by default — CRF and `-cq` are different scales, so the
  twin encode differs in bitrate. Any quality claim needs a frame-by-frame comparison.
* Kaggle GPU availability is variable; a slow run may reflect a shared machine rather
  than the pipeline.
