# Compatibility notes: production ClipWright vs. the Kaggle bridge

Everything here is a *known* divergence between `backend/src` (production) and
`kaggle-bridge/processor` (prototype), why it exists, and what would have to happen
before Phase 2 could route real jobs through Kaggle.

The rule for Phase 1 was: **copy the production contract, don't invent a new one.**
Where the two cannot match yet, it is recorded here instead of silently changed.

---

## 1. Two distributions expose a module called `whisper`

**Symptom.** `import whisper` may resolve to the legacy PyPI package `whisper`
(1.1.10, a TensorFlow-era project) instead of `openai-whisper`. The two have
incompatible APIs, and `whisper.load_model` fails with confusing attribute errors.

**Production.** `backend/src/media/common.py:38` does a plain `import whisper` and
never checks provenance. In the Docker image only `openai-whisper` is installed, so
it works by construction rather than by detection.

**Bridge.** `requirements.txt` pins `openai-whisper==20250625` and installs no other
`whisper*` distribution. `processor/transcription.py::whisper_environment()` and
`assert_openai_whisper()` then *verify* it: they read both distributions'
`entry_points.txt`. If `whisper.__file__` does not point inside `openai-whisper`,
the bridge raises instead of transcribing with the wrong library.

**Action for Phase 2.** Add the same provenance assertion to the backend startup
log (cheap, non-breaking) so a mis-built image says so once, at boot, instead of at
the first transcription.

---

## 2. `format_ms_to_timestamp` drops the hours field

**Production.** `backend/src/media/transcription.py:830` formats `HH:MM:SS`-less
timestamps: minutes are unbounded, so 3 725 000 ms renders as `62:05`. For media
longer than 59:59 the prompt lines contain timestamps that cannot be distinguished
from a 62-minute mark in a different position, and two different spans can render
identically.

**Bridge.** `processor/ai_inference.py::format_ms_to_timestamp` keeps minutes
unbounded *for parity* — the prompt text is byte-identical to what production sends,
which is what a fidelity prototype needs — but
`processor/transcription.py` records the same behaviour as a defect, and
`TRANSCRIPT_SPAN_RE` in production already accepts a three-field `HH:MM:SS` form, so
the parser can read what production cannot currently emit.

**Decision.** Not changed in Phase 1: changing the formatter changes what the model
sees, which changes selection quality — that is a production change with its own
evaluation, not a bridge detail.

**Action for Phase 2.** Emit `H:MM:SS` when the media is ≥ 1 hour, keep `MM:SS`
otherwise, and add a unit test per branch. Safe because the production regex
already tolerates both.

---

## 3. Captions are a plain reference ASS, not ClipWright's templates

**Production.** Caption styling lives in the editor/rendering path with per-word
highlighting, pop/karaoke variants and template-specific ASS chunking.

**Bridge.** `kernel/clipwright_worker.py::write_ass_captions` writes a two-word-per-
line ASS file with one fixed style. It exists to prove the caption *timings* survive
the round trip, not to reproduce the look. `rendering.captions` defaults to `false`
in the sample job for that reason.

**Action for Phase 2.** Either port the template builder into `processor/` (it is
mostly string assembly, so this is realistic) or render captions on the Oracle side
after transfer, and keep Kaggle for encode-only work. Decide with a visual diff, not
by eye.

---

## 4. ffmpeg feature availability

The Kaggle image ships a recent ffmpeg, and the bridge asserts what it actually has
rather than assuming:

* `probe_ffmpeg()` records version, `--enable-*` flags, and the encoders it cares
  about (`libx264`, `h264_nvenc`, `aac`);
* `probe_nvenc()` does a real 1-second test encode instead of trusting
  `ffmpeg -encoders` (the flag can be compiled in while the driver refuses);
* a filter the image lacks (`subtitles`, `ass`, `crop`, `tpad`, `concat`) is
  reported in `environment.json` and the affected gate fails with the filter name.

Pipeline encoders must reject an input that is not decodable — the T4 path is
encode-only, no NVENC *decode* support is assumed.

**Action for Phase 2.** Compare `environment.json` against the production image's
`ffmpeg -version` output once and pin the Kaggle dataset's ffmpeg to match, so a clip
rendered on Kaggle is byte-stable with the same clip rendered locally.

---

## 5. YouTube: no PO-token provider, no deno

**Production.** Downloads run through `bgutil-ytdlp-pot-provider` plus a local deno
for the `nsig`/PO-token challenges. That is a two-process toolchain with its own
maintenance.

**Bridge.** `requirements.txt` installs `yt-dlp` only. `processor/downloader.py`
detects the PO-token ("Sign in to confirm you're not a bot", `nsig`, HTTP 403 with
`reason=token`) failure mode and maps it to an actionable message — it does **not**
try to work around it, does not use paid proxies, and does not retry through
third-party mirrors.

**Consequence.** YouTube downloads will usually fail on a Kaggle notebook unless the
provider is installed. The container does have internet, so a future iteration can
add `bgutil-ytdlp-pot-provider` + deno to the code dataset exactly as production
does. Until then, Kaggle jobs use `source.type: "local_file"`, which is what the
sample job does and what the benchmark uses.

**Action for Phase 2.** Only add the provider if real jobs need YouTube; shipping a
video into Kaggle as a private dataset is the auditable path and avoids the
challenge entirely.

---

## 6. Long transcripts: windowing and rerank not yet ported

**Production.** `ai.py` splits the transcript into windows
(`ANALYSIS_SINGLE_WINDOW_MAX_SECONDS`, overlapping steps), analyses each, merges
candidates and reranks them.

**Bridge.** `run_analysis` sends the whole transcript in one window. That is correct
for the Phase 1 test clip (≤ 10 minutes of speech) and *wrong* for a 90-minute
episode: a 3B model with `num_ctx 8192` will truncate or hallucinate.

`ai-selection.json` records the prompt character count and the number of attempts.
Phase 1 therefore *measures* the limit instead of guessing at it.

**Action for Phase 2.** Port `split_transcript_into_windows` + merges verbatim
(each is a pure function over the transcript list), port `rerank_candidates`, and
add a long-transcript fixture to the tests. This is the biggest remaining fidelity
gap and should be closed before Kaggle handles real episodes.

---

## 7. Inference runtime differs

Production uses BAML/pydantic-ai clients. The bridge uses a thin Ollama
`/api/chat` client (`format: "json"`, `num_ctx 8192`, 180 s timeout) and a
`transformers` fallback (`unsloth/Llama-3.2-3B-Instruct`, fp16, CUDA). Prompts and
the JSON contract are identical; the transport is not.

`build_backend("auto")` tries Ollama first, then transformers, and reports every
backend it tried — so a missing model is a clear error listing both reasons rather
than a silent switch to a cloud model. There is **no** hosted fallback anywhere in
the bridge.

---

## 8. Not touched, deliberately

`docker-compose.yml`, the backend, the frontend, the database schema, the ARQ
workers and local media storage were not modified for Phase 1. The bridge reads
production *source* to copy contracts; it never imports it, and no production file
has a `kaggle` import.

---

## 9. Kaggle mount layout is not stable, and the bridge stops guessing

A dataset's mount point comes from its **slug**, not from the uploaded archive's
name, and whether the archive's top-level folder survives depends on how it was
uploaded. The kernel used to search a fixed list of shapes and take the first hit;
two datasets that both contained a `processor` directory, or a second `.mp4`
mounted for another reason, could silently win.

`processor/discovery.py` now bounds the search (depth 4, no symlinks, caches
skipped), requires `processor/__init__.py` before trusting a directory, validates a
`job.json` candidate against `clipwright.kaggle.job/1`, and refuses to choose
between multiple videos when the declared filename matches none. Every failure
message lists what was searched. `tests/test_discovery.py` models the mount tree, so
a layout change in Kaggle surfaces as a failing test rather than a wasted GPU run.

**Manifest precedence**, in order: explicit `--job`; a real `job.json` (in either
dataset) before the `sample-job.json` shipped inside the code dataset; an explicit
ambiguity error when two or more valid `job.json` files exist; the bundled example
only as a last resort (with a logged warning). Runs in `/kaggle/working` are scanned
at the top level only, because the kernel writes its own copy of `job.json` into
`/kaggle/working/clipwright-output/` — a recursive scan made a second run in the
same kernel treat its own output as a competing manifest. That was found by running
the mock mount three times in a row, not by reading the code.

Still not verifiable here: the exact mount depth Kaggle uses for a *private*
dataset created through the web UI. The bounded search covers the two shapes seen in
public examples; the first real run's `environment.json` and log will confirm which
one applied.

---

## 10. Dependency bootstrap is new behaviour, not a production contract

Production has no equivalent stage: its Docker images are built from a lockfile, so
"is openai-whisper importable?" is answered at build time. A Kaggle kernel starts
from a mutable image and a dataset that only *contains* a `requirements.txt`, so the
bridge installs at run time (`processor/dependencies.py`, Gate A0).

Where it deliberately differs from a naive `pip install -r requirements.txt`:

* **torch is never installed.** The Kaggle image's CUDA build is the only reason the
  benchmark means anything; a PyPI wheel would replace it. If torch is missing the
  run fails and says so, rather than "fixing" it.
* **only missing packages are installed**, with
  `--upgrade-strategy only-if-needed`, so a warm image is untouched.
* **verification happens after installation**, including CUDA device count and
  Whisper provenance. A transitive dependency (openai-whisper pulls torch when it is
  absent) can still alter the environment; the before/after CUDA comparison turns
  that into a hard failure instead of a silent CPU run.
* **install output is redacted** before it is stored or printed; pip prints index
  URLs, and those can carry a token.

`pytest` is in `requirements.txt` because CI and the dataset share one file; the
bootstrap installs it in a Kaggle run only because it is missing there. It is not
used by the pipeline. Splitting the files would be tidier, but a second file is a
second thing to get out of sync, and the cost is a few seconds once per kernel.
