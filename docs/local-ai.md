# Local AI: Whisper, Ollama and the offline guarantee

ClipWright Studio runs its AI on your machine. This page explains exactly which
models are used, where they are stored, how the app behaves when they are
missing, and how to tune them for a CPU-only laptop.

## The two models

| Job | Runtime | Default model | Format | Size on disk | Where it lives |
|---|---|---|---|---|---|
| Speech → word-timed transcript | `openai-whisper` (`whisper` Python package, PyTorch CPU) | `small` | `.pt` PyTorch checkpoint, SHA-256 verified against OpenAI's manifest | ~466 MB (`small`), ~142 MB (`base`) | `WHISPER_MODEL_DIR` → `./data/models/whisper/small.pt` (in containers: `/models/whisper/small.pt`) |
| Transcript → clip selection, hook titles, AI editing | Ollama (`llama3.2:3b`) | `llama3.2:3b` | Ollama's own GGUF blob store | ~2.0 GB | Ollama's store: `%USERPROFILE%\.ollama\models` on Windows, `~/.ollama/models` on macOS/Linux, or `./data/ollama` when Ollama runs as a compose service |

**These formats are not interchangeable.** The Whisper path needs a PyTorch
`.pt` checkpoint and will not load a GGUF file; Ollama needs a GGUF blob and
cannot load `.pt`. The setup script downloads exactly the right artefact for
each runtime.

`WHISPER_MODEL_SIZE` accepts `tiny`, `base`, `small`, `medium`, `large-v3`,
`large-v3-turbo` (and the `.en` variants). The value genuinely selects a
different checkpoint: the loader is keyed by model name, so changing the size
loads a different file on the next job — see
`backend/tests/unit/test_transcription_local.py::test_model_cache_is_keyed_by_size`.

## How offline mode works

`OFFLINE_MODE=true` (the default) means:

1. **No hosted AI is contacted.** `LLM=ollama:*` is the only provider that will
   run. Setting `LLM=openai:…` while offline produces a clear error instead of a
   request (`src/ai.py::_get_missing_llm_key_error`).
2. **No silent fallback.** The transcription dispatcher routes `whisper`,
   `youtube_captions` and `assemblyai` explicitly; an unknown or hosted provider
   raises instead of quietly uploading audio (`src/media/transcription.py`).
3. **No downloads during a job.** Before Whisper loads, the checkpoint must
   already exist in `WHISPER_MODEL_DIR`. Missing → actionable error, never an
   HTTP fetch. Model downloads only happen through
   `scripts/setup_local_ai.py --install-whisper` / `ollama pull`.
4. **The worker preflights.** `preflight_task()` checks the Whisper checkpoint,
   the Ollama daemon + model, and FFmpeg before the pipeline starts, so a task
   fails in seconds with the fix rather than twenty minutes in.
5. **The UI reports the truth.** `GET /local-ai/status` performs live probes and
   the Settings panel renders the result — there is no hardcoded green dot.

To opt back into hosted AI you must do both:

```dotenv
OFFLINE_MODE=false
ALLOW_HOSTED_AI=true
LLM=openai:gpt-5.2      # plus OPENAI_API_KEY
```

## The Offline Mode switch

`OFFLINE_MODE` in `.env` is the deploy default. **Settings → Offline & local AI**
also has a switch that persists a runtime override, because "stop sending
anything anywhere right now" should not require editing files and restarting:

```
<data>/config/offline-mode.json   -> {"offline_mode": false}
```

* Compose mounts that directory as `/config` into `backend`, `worker` and
  `ai-worker`, so every process reads the same answer and the choice survives a
  restart or a rebuild.
* The switch can only *tighten* the guarantee on its own: turning offline mode
  off does not enable a hosted provider. That still needs `ALLOW_HOSTED_AI=true`
  and a configured key, and the panel says so.
* "Use the .env default" clears the file and returns to the `OFFLINE_MODE` value.
* A missing, unreadable or non-boolean file is ignored with a warning; it never
  breaks a job.

The identical `.env` route still works for headless installs — set
`OFFLINE_MODE=false` and `ALLOW_HOSTED_AI=true`, restart, done.

## Docker networking to Ollama

Ollama normally runs natively on Windows, outside Docker. The backend and both
workers therefore default to:

```
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
```

`docker-compose.yml` adds `extra_hosts: host.docker.internal:host-gateway` to
`backend`, `worker` and `ai-worker`, which also makes this work on Linux.
Outside Docker the default is `http://localhost:11434/v1`.

If you would rather run Ollama in Docker:

```bash
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull llama3.2:3b
# .env
OLLAMA_BASE_URL=http://ollama:11434/v1
```

Models are stored in `./data/ollama`, so a container restart never re-downloads
them. Names inside the compose network resolve; from the browser-side preview
nothing needs to reach Ollama directly — the backend does the talking.

## CPU tuning (Ryzen 5 4600U / 16 GB, no GPU)

* **Transcription is the slow part.** `small` on this CPU runs roughly 0.3–0.6×
  real time with `int8`-free PyTorch CPU inference — a 20-minute source is
  typically 35–70 minutes. Switch to `base` for ~2× speed at some accuracy cost:
  `WHISPER_MODEL_SIZE=base`, then
  `python scripts/setup_local_ai.py --install-whisper --whisper-model base`.
* **`llama3.2:3b` is the right size for 16 GB.** A 3B model needs ~2.5–3.5 GB
  while it answers; a 7B model fits but competes with FFmpeg renders. Keep the
  default unless you have measured the difference.
* **Context.** Analysis runs in overlapping ~12-minute windows with a rerank
  pass, so a 90-minute source never has to fit in one context window. AI editing
  uses `num_ctx: 8192` and `num_predict: 2048` against the same model.
* **Render alongside analysis, not during transcription.** CPU inference and
  x264 encoding both want all the cores.
* **Memory:** one Whisper model in RAM (~1 GB for `small`) plus one Ollama model
  (~3 GB) plus FFmpeg (~1–2 GB) fits comfortably in 16 GB. Avoid loading
  `medium` or `large` on this machine.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Settings shows "Ollama is not reachable" | Daemon not running | Start Ollama (system tray on Windows) or `ollama serve` |
| "model 'llama3.2:3b' is not installed" | Model never pulled | `ollama pull llama3.2:3b` |
| Docker backend cannot reach the host daemon | Ollama bound to loopback only | Set `OLLAMA_HOST=0.0.0.0:11434` for the Ollama service, or keep `host.docker.internal` and allow it through the Windows firewall |
| "Whisper model 'small' is not installed" | Never downloaded | `python scripts/setup_local_ai.py --install-whisper` |
| Task errors with "Local AI is not ready: …" | Preflight caught a missing model | Run `python scripts/setup_local_ai.py --check` |
| Transcription painfully slow | `small` on CPU | Switch to `base` (see above) |
| "AssemblyAI … disabled because ClipWright Studio runs offline" | A hosted provider is configured | `TRANSCRIPTION_PROVIDER=whisper` |
| Settings says "This backend does not report local AI status yet" | Old backend image | `docker compose up -d --build backend worker` |

## Optional internet-dependent features

These are the only parts of the product that need a connection. Each is labelled
"online only" in Settings → Offline & local AI.

| Feature | Why it needs the internet | Offline behaviour |
|---|---|---|
| YouTube / Twitch / Kick downloads | Fetches the media | Disabled with a clear error; local files always work |
| Pexels stock B-roll | Stock search API | No new B-roll; existing B-roll still renders |
| Social publishing & analytics | Platform APIs | Hidden/disabled; download and upload manually |
| Hosted AI providers | External inference | Refused in offline mode |
| Product-update email / feedback form | External services | Non-essential; the app works without them |

Everything else — import, transcribe, analyse, edit, caption, reframe, render,
export, save, reopen, resume — is local.
