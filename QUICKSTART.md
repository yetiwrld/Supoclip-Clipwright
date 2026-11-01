# ClipWright Studio — Quick Start

Ten minutes from clone to first clip, with no API keys and no internet access
during processing.

## Prerequisites

1. **Docker Desktop** installed and running (Windows 11 Pro: enable WSL 2).
2. **Ollama** installed — <https://ollama.com/download>. It runs as a normal
   Windows app, outside Docker.
3. ~6 GB free disk for images, models and working space.

No AI accounts, API keys, credit cards or cloud subscriptions are needed.

## One command

```bash
./start.sh
```

The script checks Docker, the Whisper model directory and `ollama list`, builds
the images, starts the stack and prints the URLs. It never asks for a hosted API
key — if it finds one configured it warns you that offline mode is off.

## First-time setup

Everything that downloads weights is explicit and one-time:

```powershell
# Windows 11 (PowerShell) — checks tools, writes .env, pulls llama3.2:3b and Whisper "small"
powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -InstallModels
```

```bash
# macOS / Linux
cp .env.example .env
python3 scripts/setup_local_ai.py --write-env --install-whisper --pull-ollama-model
docker compose up -d --build
```

Already have the model files? Skip `-InstallModels`; nothing will be fetched and
the app still starts, reporting the missing model in Settings → Offline & local AI.

### What goes where

| Thing | Default | Change with |
|---|---|---|
| Whisper checkpoint | `./data/models/whisper/small.pt` | `WHISPER_MODEL_SIZE`, `WHISPER_MODEL_DIR` |
| Ollama model | Ollama's own store (`llama3.2:3b`) | `ollama pull <model>` + `LLM=ollama:<model>` |
| Uploads / clips | `./data/uploads`, `./data/clips` | `CLIPWRIGHT_DATA_DIR` |
| Database | Docker volume `postgres_data` | `POSTGRES_*` in `.env` |

Nothing in `./data` is deleted by `docker compose down`.

## Environment variables you may actually edit

```dotenv
OFFLINE_MODE=true               # keep true: no hosted AI calls, ever
TRANSCRIPTION_PROVIDER=whisper
WHISPER_MODEL_SIZE=small        # "base" is ~3x faster on CPU
LLM=ollama:llama3.2:3b
AI_EDIT_MODEL=ollama:llama3.2:3b
```

Everything else has a working default. The full annotated list is in
`.env.example` and [docs/configuration.md](docs/configuration.md).

## Access the app

- **Frontend**: <http://localhost:3107>
- **Backend API**: <http://localhost:8000>
- **API docs**: <http://localhost:8000/docs>
- **Local AI status**: <http://localhost:8000/local-ai/status>

These URLs assume the copied `.env.example`, which sets `FRONTEND_PORT=3107`.
Existing `.env` files without that setting keep Docker host port `3001`. Keep
your existing port if a reverse proxy points at it.

## Manual Docker commands

```bash
docker compose up -d --build     # start / rebuild
docker compose stop              # stop, keep data
docker compose restart backend worker ai-worker
docker compose logs -f worker    # watch the pipeline
docker compose down              # remove containers, keep volumes and ./data
```

## Verify the local models

```bash
python scripts/setup_local_ai.py --check    # Docker, FFmpeg, Ollama, models, backend health
ollama list                                 # what Ollama has
curl http://localhost:8000/local-ai/status  # what the backend sees
```

## First clip

1. Open <http://localhost:3107> and create an account (stored locally in
   PostgreSQL — no email is sent).
2. **Create** → drop a local video file (MP4/MOV/MKV/WebM) or paste a YouTube
   link (online only).
3. Choose the clip count and aspect, then start. Whisper transcribes on CPU,
   Ollama picks the moments, FFmpeg renders.
4. Edit the clips in the studio, then download or export.

A 20-minute source on a Ryzen 5 4600U takes roughly 35–70 minutes with
`WHISPER_MODEL_SIZE=small`, or about half that with `base`. Analysis and
rendering add a few minutes.

## Troubleshooting in one line each

- **"Local AI is not ready"** → `python scripts/setup_local_ai.py --check`.
- **Ollama unreachable from Docker** → start Ollama on the host; compose already
  routes via `host.docker.internal`.
- **Whisper model missing** → `python scripts/setup_local_ai.py --install-whisper`.
- **Port already in use** → change `FRONTEND_PORT` / `BACKEND_PORT` in `.env`.

More: [docs/troubleshooting.md](docs/troubleshooting.md) and
[docs/local-ai.md](docs/local-ai.md).
