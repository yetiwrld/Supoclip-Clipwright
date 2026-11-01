# Setup

This guide covers the recommended Docker setup, local development mode, and the checks to perform after first boot.

## Requirements

### Required software

- Docker Desktop or a Docker Engine installation with Compose support
- Git

### Required credentials

**None.** ClipWright Studio transcribes with a local Whisper checkpoint and
analyses with a local Ollama model. See [local-ai.md](local-ai.md) for the model
formats, sizes and storage paths, and `scripts/setup_local_ai.py` for the
download steps.

### Optional: hosted AI instead of local AI

Only used when you explicitly set `OFFLINE_MODE=false` **and**
`ALLOW_HOSTED_AI=true`. Nothing below is needed for local operation.

- `ASSEMBLY_AI_API_KEY` with `TRANSCRIPTION_PROVIDER=assemblyai`
- One hosted LLM provider configuration:
  - `OPENAI_API_KEY` with `LLM=openai:...`
  - `GOOGLE_API_KEY` with `LLM=google-gla:...`
  - `ANTHROPIC_API_KEY` with `LLM=anthropic:...`
  - `OPENROUTER_API_KEY` with `LLM=openrouter:<vendor>/<model>`

### Optional credentials

- `PEXELS_API_KEY` for AI B-roll sourcing
- `NEXT_PUBLIC_DATAFAST_WEBSITE_ID` and `NEXT_PUBLIC_DATAFAST_DOMAIN` for DataFast analytics
- `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `SES_FROM_EMAIL` for hosted billing emails
- Stripe keys if you are running with monetization enabled
- Discord webhook URLs for feedback forwarding

## Recommended Setup: Docker

Docker is the intended path for running ClipWright Studio because it starts the frontend, backend, worker, PostgreSQL, and Redis together with the expected wiring.

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd clipwright
```

### 2. Create a local environment file

```bash
cp .env.example .env
```

Then edit `.env` and set at least:

```env
# Local AI (defaults that ship in .env.example)
OFFLINE_MODE=true
TRANSCRIPTION_PROVIDER=whisper
WHISPER_MODEL_SIZE=small
LLM=ollama:llama3.2:3b
AI_EDIT_MODEL=ollama:llama3.2:3b
BETTER_AUTH_SECRET=replace_this_for_real_use
BACKEND_AUTH_SECRET=replace_this_if_using_hosted_mode

# Optional: DataFast analytics
NEXT_PUBLIC_DATAFAST_WEBSITE_ID=dfid_xxxxx
NEXT_PUBLIC_DATAFAST_DOMAIN=your-domain.com
NEXT_PUBLIC_DATAFAST_ALLOW_LOCALHOST=false
```

Before starting, download the models once (explicit, never at app startup):

```bash
python scripts/setup_local_ai.py --write-env --install-whisper   # Whisper .pt into ./data/models/whisper
ollama pull llama3.2:3b                                          # Ollama GGUF into Ollama's store
```

### 3. Start the stack

Fastest option:

```bash
./start.sh
```

Manual equivalent:

```bash
docker-compose up -d --build
```

### 4. Wait for services to become healthy

```bash
docker-compose logs -f
docker-compose ps
```

You should see these services:

- `clipwright-frontend`
- `clipwright-backend`
- `clipwright-worker`
- `clipwright-ai-worker`
- `clipwright-postgres`
- `clipwright-redis`

(SupoClip-era container names were `supoclip-*`; scripts that target the old
names need updating — see [REBRANDING.md](REBRANDING.md).)

### 5. Open the application

- Frontend: `http://localhost:3107`
- Backend API: `http://localhost:8000`
- FastAPI docs: `http://localhost:8000/docs`

The template sets `FRONTEND_PORT=3107`. Existing `.env` files without this
setting retain the legacy Docker host port `3001`; use `http://localhost:3001`
or add `FRONTEND_PORT=3107` and recreate the frontend. The container always
listens on `3107`.

For production, preserve the port your reverse proxy uses (for example,
`FRONTEND_PORT=3001`). Keep `NEXT_PUBLIC_APP_URL`, `BETTER_AUTH_URL`, and
`CORS_ORIGINS` set to your public HTTPS origin. For local custom ports, set
those URLs/origins to match the port you chose. Changing `NEXT_PUBLIC_*`
values for a production image requires rebuilding the frontend.

### Verify the local AI stack

Host facts are not enough — the containers have their own network namespace and
mounts — so readiness is only reported after every running service passes the
in-container probe:

```bash
python scripts/verify_local_ai.py             # backend + worker + ai-worker matrix
docker compose exec -T backend .venv/bin/python -m src.local_ai --check
python scripts/setup_local_ai.py --check      # host tools + the container verdict
ollama list                                   # llama3.2:3b must be listed
ls -lh data/models/whisper                    # small.pt must be present
```

A failure names the component and the service, for example
`worker: ollama_reachable — Ollama is not reachable at
http://host.docker.internal:11434`. On Windows that almost always means Ollama
is bound to loopback only; see
[local-ai.md](local-ai.md#windows-making-native-ollama-reachable-from-docker)
and `scripts/setup-ollama-windows.ps1`.

The same information is rendered in the app under **Settings → Offline & local
AI**. A ready install shows Ollama reachable, the configured LLM model present,
Whisper available and readiness "ready". Anything else comes with the exact
command that fixes it.

## What Docker Starts

### CPU-only Docker builds (including Apple Silicon)

The current template sets `BACKEND_CPU_ONLY=true`, selecting the locked
CPU-only Torch wheels for both backend and worker. Existing `.env` files
without the setting keep the legacy CUDA dependency path. To opt in:

```env
BACKEND_CPU_ONLY=true
```

Then run `docker compose up -d --build backend worker`. Changing
`TRANSCRIPTION_PROVIDER` alone does not change the installed Torch variant.
`BACKEND_CPU_ONLY` avoids NVIDIA downloads. It does not enable Apple GPU
acceleration. GPU deployments should retain `BACKEND_CPU_ONLY=false` and their
existing GPU runtime configuration. Local Python users can select
`uv sync --extra cpu` or `uv sync --extra cuda`; these extras are mutually
exclusive.

Backend and worker images default to `linux/amd64` (`BACKEND_PLATFORM`). On
Apple Silicon, Docker runs that image under emulation. yt-dlp's YouTube
metadata step can then take around 15 seconds, so task creation waits up to
`YOUTUBE_PREFLIGHT_TIMEOUT_SECONDS` (default 30). If valid YouTube links still
report that the length check timed out, set the timeout higher (for example
`60`) and restart the backend.

To build those two services for the host architecture, set
`BACKEND_PLATFORM=linux/arm64` and `BACKEND_CPU_ONLY=true`, then run
`docker compose up -d --build backend worker`. CUDA wheels are published for
amd64, so GPU machines should keep `BACKEND_PLATFORM=linux/amd64`. Native
arm64 images are best-effort: this does not publish a multi-arch image and
does not add Apple GPU acceleration.

The default Compose stack contains five services:

- `frontend`
  - Next.js application on port `3107`
  - Proxies authenticated requests to the backend
- `backend`
  - FastAPI API on port `8000`
  - Provides task, media, billing, admin, and feedback endpoints
- `worker`
  - ARQ background worker
  - Processes long-running video jobs from Redis
- `postgres`
  - Stores users, sessions, tasks, sources, clips, billing metadata, and auth rotation state
- `redis`
  - Backs the job queue and progress event flow

## First-Run Checklist

After the stack is up:

1. Load the homepage at `http://localhost:3107`.
2. Create an account or sign in.
3. Submit a YouTube, Twitch, or Kick URL, or upload a video file.
4. Open the task page and confirm progress updates appear.
5. Wait for clip generation to finish.
6. Open the clips list and verify playback and download work.
7. If DataFast is enabled, open browser devtools and confirm `/js/script.js` and `/api/events` load from your own domain.
8. Trigger one successful action such as sign-up, sign-in, task creation, or feedback submission and verify the goal arrives in DataFast.

## Local Development Without Docker

Use this mode if you need to iterate on a single app directly. You still need PostgreSQL and Redis running somewhere.

### Backend

```bash
cd backend
uv venv .venv
source .venv/bin/activate
uv sync
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
cd backend
source .venv/bin/activate
arq src.workers.tasks.WorkerSettings
```

### Frontend

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm run dev
```

### Required local dependencies

- Python 3.11+
- Node.js compatible with Next.js 15
- PostgreSQL
- Redis
- FFmpeg available to the backend environment

## Data and Volumes

With Docker, ClipWright Studio stores persistent data in named volumes:

- `postgres_data`
- `redis_data`
- `uploads`
- `clips`

The backend also mounts these local directories:

- `backend/fonts`
- `backend/transitions`

## Hosted Mode Versus Self-Hosted Mode

ClipWright Studio defaults to self-host mode:

```env
SELF_HOST=true
```

When `SELF_HOST=false`, monetization and hosted billing flows become active. That mode requires additional Stripe and backend auth configuration. See [Configuration](./configuration.md).

## Production Setup Notes

For anything beyond local experimentation:

- Change `BETTER_AUTH_SECRET`
- Set a strong `BACKEND_AUTH_SECRET`
- Put the app behind HTTPS
- Set `NEXT_PUBLIC_APP_URL` to your deployed frontend origin
- Use persistent storage and backups for PostgreSQL
- Keep API keys outside version control
- Decide whether you want self-host mode or monetized hosted mode before launch
- Verify all callback URLs and origins match your deployed domain
- If using DataFast, set `NEXT_PUBLIC_DATAFAST_DOMAIN` to the deployed root domain you want tracked
- For hosted billing, create and verify the Stripe prices before deploy: Pro at `$9.99/month` and `$99.90/year`, Creator (`scale`) at `$24.99/month` and `$249.90/year`; keep the earlier Pro price in `STRIPE_PRO_LEGACY_PRICE_IDS`. Add the yearly prices to the customer portal's plan-switching products so subscribers can move between them

## Useful Commands

### Start or rebuild

```bash
docker-compose up -d --build
```

### Stream logs

```bash
docker-compose logs -f
docker-compose logs -f backend
docker-compose logs -f worker
```

### Stop services

```bash
docker-compose down
```

### Reset containers and volumes

```bash
docker-compose down -v
docker-compose up -d --build
```

Warning: `docker-compose down -v` deletes the PostgreSQL and Redis volumes
(projects, accounts, queue). Imported media, rendered clips and model files live
in the bind-mounted `./data` tree and survive.

### Where data lives

| Path | Contents | Survives `down` | Survives `down -v` |
|---|---|---|---|
| `./data/uploads` | Source media, transcription caches | yes | yes |
| `./data/clips` | Rendered clips and exports | yes | yes |
| `./data/models/whisper` | Whisper checkpoints | yes | yes |
| `./data/ollama` | Ollama blobs (compose profile only) | yes | yes |
| volume `postgres_data` | Projects, tasks, transcripts, edits | yes | no |
| volume `redis_data` | Job queue state | yes | no |

Set `CLIPWRIGHT_DATA_DIR` to relocate the whole `./data` tree; see
[media-storage.md](media-storage.md).

## Next Steps

- Review [Configuration](./configuration.md) before changing defaults
- Review [App Guide](./app-guide.md) to understand the UI and workflows
- Review [Troubleshooting](./troubleshooting.md) if tasks do not process correctly
