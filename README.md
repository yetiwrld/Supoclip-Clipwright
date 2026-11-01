<div align="center">

<img src="frontend/public/brand/clipwright-logo.png" alt="ClipWright Studio" width="140" />

# ClipWright Studio

**Local-first AI video clipping. Whisper and Ollama on your own machine.**

No AI API keys. No uploads. No per-minute billing.

</div>

---

ClipWright Studio turns long videos into short, captioned vertical clips. It
runs the whole pipeline on your computer: a local **Whisper** model writes the
transcript, a local **Ollama** model (`llama3.2:3b` by default) picks the
moments worth posting, and **FFmpeg** renders the result. Your footage,
transcripts and prompts never leave the machine.

> **Provenance.** ClipWright Studio is a rebrand and local-first transformation
> of the open-source [SupoClip](https://github.com/FujiwaraChoki/supoclip)
> project (AGPL-3.0). The original copyright and licence notices are retained.
> See [docs/REBRANDING.md](docs/REBRANDING.md) for exactly what was renamed and
> which technical identifiers were deliberately kept.

## What you get

- **Offline transcription** — OpenAI Whisper (`small` by default, `base` if you
  want it faster) with word-level timestamps, cached on disk.
- **Local clip selection** — Ollama `llama3.2:3b` reads the transcript, scores
  hooks/engagement/value/shareability and proposes clips. No cloud fallback.
- **Local AI editing** — the same model turns "cut the filler and zoom in" into
  typed, validated editor operations.
- **A real editor** — timeline trim/split/merge, word-level transcript fixes,
  caption styling, reframing, effects, hook titles, export presets.
- **Persistent projects** — sources, transcripts, clips, edits and renders live
  in `./data` and survive restarts and container rebuilds.
- **A dark, studio-grade interface** — charcoal surfaces, orange accent drawn
  from the app icon, consistent tokens across landing, auth, library, editor and
  dialogs.

Optional online features (YouTube/Twitch/Kick downloads, Pexels B-roll, social
publishing) are clearly labelled in the UI and are never required.

## Requirements

| | |
|---|---|
| OS | Windows 11 Pro (developed against), macOS, Linux |
| CPU | Any x86-64 CPU. Tested profile: AMD Ryzen 5 4600U, 16 GB RAM, integrated Radeon |
| GPU | **Not required** — no CUDA, no ROCm |
| Docker | Docker Desktop 4.x with Compose v2 |
| Ollama | [ollama.com/download](https://ollama.com/download) |
| Disk | ~2 GB for the Docker images, ~0.5 GB (`small`) or ~0.15 GB (`base`) for Whisper, ~2 GB for `llama3.2:3b` |

## First-time setup (Windows 11, PowerShell)

```powershell
git clone https://github.com/yetiwrld/Supoclip-Clipwright.git clipwright
cd clipwright

# 1. Check the machine, write .env, download the models (explicit, one time)
powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -InstallModels

# 2. Open the studio
start http://localhost:3107
```

`-InstallModels` is the only step that downloads anything: it pulls
`llama3.2:3b` into Ollama's store and the Whisper checkpoint into
`./data/models/whisper`. Without it, setup only checks what is missing.

Prefer doing it by hand?

```powershell
copy .env.example .env
python scripts\setup_local_ai.py --write-env        # local-AI defaults, never overwrites
python scripts\setup_local_ai.py --install-whisper  # ~466 MB for "small"
ollama pull llama3.2:3b                             # ~2 GB
docker compose up -d --build
python scripts\setup_local_ai.py --check            # verifies everything, incl. /health
```

macOS / Linux:

```bash
cp .env.example .env
python3 scripts/setup_local_ai.py --write-env
python3 scripts/setup_local_ai.py --install-whisper
ollama pull llama3.2:3b
docker compose up -d --build
```

## Everyday commands

```bash
docker compose up -d              # start
docker compose stop               # stop (state and projects kept)
docker compose restart backend worker ai-worker
docker compose down               # remove containers, keep ./data and volumes
docker compose logs -f backend    # logs
```

The app is at <http://localhost:3107>, the API at <http://localhost:8000>
(interactive docs at `/docs`).

## Verify the local models

```bash
# One-shot report: Docker, FFmpeg, Ollama, model presence, backend health
python scripts/setup_local_ai.py --check

# What Ollama actually has
ollama list

# What the running backend sees (live probe, same call the Settings panel makes)
curl http://localhost:8000/local-ai/status

# Whisper checkpoint on disk
dir data\models\whisper     # Windows
ls -lh data/models/whisper  # macOS/Linux
```

Inside the app: **Settings → Offline & local AI** shows the same values, with a
red/amber row and the exact command for anything that is missing, plus the
Offline Mode switch (persisted to `./data/config/offline-mode.json`, honoured by
the API and both workers).

## Where your data lives

| Path | Contents |
|---|---|
| `./data/uploads/` | Imported source media and transcription caches |
| `./data/clips/` | Rendered clips and exports |
| `./data/models/whisper/` | Whisper checkpoints (`small.pt`, `base.pt`, …) |
| `./data/ollama/` | Only used when you run Ollama as a compose service |
| Docker volume `postgres_data` | Project/task database (accounts, transcripts, edits) |
| Docker volume `redis_data` | Job queue |

Uploads, clips and models are host bind mounts, so `docker compose down` (or a
rebuild) never deletes them. Set `CLIPWRIGHT_DATA_DIR=D:\clipwright-data` to
move the whole tree elsewhere.

## Configuration worth knowing

All in `.env` (see `.env.example` for the annotated list):

```dotenv
OFFLINE_MODE=true           # no hosted AI, ever
ALLOW_HOSTED_AI=false       # hosted providers need this AND OFFLINE_MODE=false
TRANSCRIPTION_PROVIDER=whisper
WHISPER_MODEL_SIZE=small    # switch to "base" if CPU transcription is too slow
WHISPER_MODEL_DIR=./data/models/whisper
LLM=ollama:llama3.2:3b      # any pulled Ollama model works, e.g. ollama:qwen2.5:7b
AI_EDIT_MODEL=ollama:llama3.2:3b
OLLAMA_BASE_URL=            # empty = auto (localhost, or host.docker.internal in Docker)
```

## Tests

```bash
cd backend && uv sync --frozen --all-groups && .venv/bin/pytest tests/unit
cd frontend && pnpm install --frozen-lockfile && pnpm test
cd frontend && pnpm lint && pnpm typecheck
```

`make test` runs backend + frontend. Browser tests (`pnpm test:e2e`) need
PostgreSQL and a generated Prisma client.

## Documentation

- [docs/local-ai.md](docs/local-ai.md) — models, storage, tuning, troubleshooting
- [docs/setup.md](docs/setup.md) — full installation reference
- [docs/configuration.md](docs/configuration.md) — every environment variable
- [docs/REBRANDING.md](docs/REBRANDING.md) — what changed from SupoClip
- [docs/editor.md](docs/editor.md), [docs/media-storage.md](docs/media-storage.md),
  [docs/architecture.md](docs/architecture.md)

## Licence

AGPL-3.0 — see [LICENSE](LICENSE). ClipWright Studio is derived from SupoClip
(Copyright the SupoClip contributors), and that attribution, together with the
AGPL-3.0 obligations, must be preserved in any redistribution.
