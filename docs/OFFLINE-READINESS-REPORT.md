# ClipWright Studio — transformation report (2026-11-01)

Scope: turn the cloned SupoClip repository into **ClipWright Studio**, an
offline-first, local-AI video clipper, without rebuilding the product.
Branch: `arena/dd48529f-supoclip-clipwright`, commit `d005671` on top of the
upstream import `e0db14c`.
Diff: 263 files, +6,178 / −2,689.

---

## 1. What changed, by area

### 1.1 Local AI runtime (backend)

| File | Change |
|---|---|
| `backend/src/whisper_models.py` *(new)* | Dependency-free catalogue of OpenAI Whisper checkpoints: name → official URL (whose directory component is the SHA-256), size in MB, `normalize_model_name`, `checkpoint_filename`, `model_url`. Import-safe without torch so the host setup script can use it. |
| `backend/src/local_ai.py` *(new)* | The single "is the local stack ready?" module: Whisper checkpoint lookup, Ollama daemon probe (`/api/tags`), model-name matching, `LocalModelUnavailable`, FFmpeg check, `preflight_task()`, `local_ai_status()`. Never contacts a hosted AI provider. |
| `backend/src/offline_mode.py` *(new)* | Persisted runtime Offline Mode override (`<config>/offline-mode.json`), mtime-cached, atomic writes, corrupt-file tolerance. |
| `backend/src/api/routes/local_ai.py` *(new)* | `GET /local-ai/status` (live probe) and `POST /local-ai/offline-mode` (strict boolean / `null`). |
| `backend/src/config.py` | Defaults changed: `TRANSCRIPTION_PROVIDER=whisper`, `WHISPER_MODEL_SIZE=small`, `WHISPER_DEVICE=cpu`, `WHISPER_MODEL_DIR`, `MODELS_DIR`, `OFFLINE_MODE=true`, `ALLOW_HOSTED_AI=false`, `LLM=ollama:llama3.2:3b`, `AI_EDIT_MODEL`, `RERANK_LLM`; `hosted_ai_enabled` gate; Docker-aware Ollama base URL; runtime override resolution. |
| `backend/src/media/transcription.py` | Whisper path loads the local `.pt` only, keyed by model size (so changing the size really loads a different file), keeps timestamps + word timings, streams audio to disk instead of holding media in RAM, actionable errors when a checkpoint is missing, no download during a job. |
| `backend/src/ai.py` | Ollama-only default; hosted-name refusal while offline with a creator-facing message; windowed analysis (~12 min) + rerank for long transcripts; timeouts (600 s/window, 1800 s total, 240 s rerank); malformed-JSON and oversize-context handling. |
| `backend/src/services/ai_edit_service.py` | AI editing against the local model (`num_ctx` 8192, `num_predict` 2048, 120 s timeout), typed operation validation, no hosted fallback. |
| `backend/src/workers/tasks.py` | `preflight_task()` before the pipeline; readiness problems surface as a task error naming the fix. |
| `backend/tests/unit/test_local_ai.py`, `test_transcription_local.py`, `test_ai_offline_guard.py`, `test_offline_mode_switch.py`, `test_local_ai_route.py` *(new)* | 22 + 9 + 11 + 10 + 4 tests covering model loading, provider selection, model-unavailable errors, no hosted fallback, and the switch. |

### 1.2 Branding (frontend, docs, packaging)

- `frontend/src/lib/brand.ts` — single source for name, tagline, description and every asset path.
- `frontend/src/components/brand-mark.tsx` — the only component that renders the logo (`unoptimized`, fixed ratio, no filters, no invert).
- 17 `<Image>`/`<img>` call sites migrated to the brand module; sidebar and mobile wordmarks rebuilt.
- `frontend/public/manifest.webmanifest` — icons from `/brand/*`, `theme_color` `#111113`, maskable 512.
- `frontend/src/app/layout.tsx` — metadata, icons (favicon, 32/192/512, apple-touch 180), manifest, static `class="dark"`.
- Removed App Store / Product Hunt surfaces and `app-store-badge.svg`; creator codes redeem via `/settings/creator-program?code=…`.
- 213 files swept for `SupoClip` → `ClipWright Studio` with ordered rules that avoided renaming private/technical identifiers (see `docs/REBRANDING.md`).
- Overlays, dialogs, toasts, empty states, error copy, emails and legal/metadata strings rebranded; `messages/en/metadata.json` rewritten; marketing copy repositioned as offline-first.

### 1.3 Dark theme

- Tokens in `frontend/src/app/globals.css` exposed as semantic Tailwind classes (`bg-background`, `bg-card`, `text-muted-foreground`, `border-border`, `text-primary`, `--success`, `--warning`…). No component-level hardcoded palettes remain outside deliberate white-on-media cases.
- `theme-provider.tsx`: `defaultTheme="dark"`, `enableSystem={false}`, storage key `clipwright-theme`; `<html class="dark">` is static so a light-OS machine still opens dark. `.light` overrides exist only for the optional light theme.
- Landing, auth, app shell, library, editor, timeline, media panel, caption editor, settings, dropdowns, modals, tables, tooltips and charts read the same tokens.

### 1.4 Storage, setup, deployment

- `docker-compose.yml`: `./data/{uploads,clips,models,config}` host bind mounts for `backend`, `worker`, `ai-worker`; `CLIPWRIGHT_CONFIG_DIR=/config`; `WHISPER_MODEL_DIR=/models/whisper`; `extra_hosts: host.docker.internal:host-gateway`; opt-in `ollama` profile with `./data/ollama`; containers renamed `clipwright-*`.
- `.env.example` rewritten for the key-free, local-first defaults.
- `scripts/setup_local_ai.py` *(new)*: `--check`, `--write-env`, `--install-whisper`, `--pull-ollama-model`; SHA-256 verified download into the persistent model directory.
- `scripts/setup-clipwright.ps1` *(new)*: Windows 11 wrapper (`-Check`, `-InstallModels`).
- `start.sh`: no hosted-key requirement, checks Whisper directory and `ollama list`, warns only if a hosted provider is configured.
- `.gitignore`: `/data/`, `/data-*/`, `/models/`, `*.pt`, virtualenvs.

---

## 2. Exact models, formats, storage

| | Transcription | Analysis / AI editing |
|---|---|---|
| Runtime | `openai-whisper` on PyTorch CPU | Ollama |
| Default | `small` (`WHISPER_MODEL_SIZE=small`) | `llama3.2:3b` (`LLM=ollama:llama3.2:3b`) |
| Format | PyTorch `.pt` checkpoint | GGUF inside Ollama's blob store |
| Size | 466 MB (`small`), 142 MB (`base`) | ~2.0 GB |
| Path | `./data/models/whisper/small.pt` → container `/models/whisper/small.pt` | Ollama store: `%USERPROFILE%\.ollama\models` (native) or `./data/ollama` (compose profile) |
| Switch | `WHISPER_MODEL_SIZE=base` + `setup_local_ai.py --install-whisper --whisper-model base` | `ollama pull <model>` + `LLM=ollama:<model>` |

Other persistent state: `./data/uploads` (sources + transcription caches),
`./data/clips` (renders), `./data/config/offline-mode.json` (runtime switch),
Docker volumes `postgres_data` / `redis_data` (projects, tasks, queue).
`CLIPWRIGHT_DATA_DIR` relocates the whole tree.

---

## 3. First-time setup (Windows 11 Pro, PowerShell)

```powershell
git clone https://github.com/yetiwrld/Supoclip-Clipwright.git clipwright
cd clipwright

# check only (no downloads)
powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -Check

# one-time downloads: llama3.2:3b (~2 GB) + Whisper small (~466 MB)
powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -InstallModels

docker compose up -d --build
start http://localhost:3107
```

Manual equivalent:

```powershell
copy .env.example .env
python scripts\setup_local_ai.py --write-env --install-whisper --pull-ollama-model
docker compose up -d --build
```

Prerequisite: Ollama installed on the host (<https://ollama.com/download>) and
running. Docker Desktop with Compose v2. No CUDA, no GPU, no API keys.

Start / stop / restart:

```bash
docker compose up -d
docker compose stop
docker compose restart backend worker ai-worker
docker compose down            # keeps ./data and both volumes
```

Verification:

```bash
python scripts/setup_local_ai.py --check
ollama list
curl http://localhost:8000/local-ai/status
dir data\models\whisper
```

In-app: **Settings → Offline & local AI** (live status + Offline Mode switch).

---

## 4. Test results (this machine, this session)

| Suite | Result |
|---|---|
| Backend unit `pytest tests/unit` | **1,491 passed**, 98 skipped, **11 failed** — all 11 also fail on the pristine upstream clone `4bc76f83` in the same sandbox (no `ffmpeg` binary, no OpenCV haar cascade model, no PostgreSQL on 5432). Not caused by this work. |
| New backend tests | `test_local_ai` 22 ✅, `test_transcription_local` 9 ✅, `test_ai_offline_guard` 11 ✅, `test_offline_mode_switch` 10 ✅, `test_local_ai_route` 4 ✅ |
| Frontend `vitest run` (whole suite) | **625 passed / 95 files** ✅ |
| Frontend `eslint src --max-warnings 0` | clean ✅ |
| Frontend `tsc --noEmit` | only missing generated Prisma client errors (`src/generated/prisma` absent because `prisma generate` cannot reach `binaries.prisma.sh` in this sandbox) |
| `pnpm build` | **not run** — `prisma generate` fails on that same blocked download; the build has not been proven in this sandbox |
| Playwright e2e (`e2e/theme-toggle.spec.ts` incl. new dark-default cases) | **not run** — browser binaries cannot be installed here; needs PostgreSQL + generated Prisma client |
| Docker Compose up / Ollama pull / Whisper download / real transcription and render | **not run** — `docker`, `ffmpeg`, `python`-torch and `ollama` do not exist in this sandbox |

Static smoke test that *was* run: `next dev` served `/`, `/sign-in`, `/blog`,
`/changelog`, `/demo`, `/sign-up` with HTTP 200, `<html class="dark">`,
`<title>ClipWright Studio – Offline AI Video Clipper (Whisper + Ollama)</title>`,
zero occurrences of `SupoClip`, `/brand/clipwright-logo.png` served byte-identical
(1,057,282 bytes = the uploaded asset), and manifest/favicon resolving.

### The sandbox preview (why it needed a workaround)

The live preview runs `next dev` against the real frontend with two
environment-only substitutions, neither of which is in the repository:

1. **`src/generated/prisma`** is gitignored (it is `prisma generate` output).
   Generation cannot run here because Prisma downloads its schema engine from
   `binaries.prisma.sh`, which this sandbox cannot reach, so every page that
   touches auth failed with `Cannot find module '../generated/prisma'`. A local
   stand-in client (no rows, no database — also gitignored) lets the marketing
   and auth screens render signed-out. On a real machine `pnpm install &&
   pnpm exec prisma generate` produces the genuine client; nothing in the repo
   or the Docker image is affected.
2. **Google Fonts** (`Geist`, `Geist Mono`, `Syne`) are unreachable here, so
   `next/font` falls back to system fonts. On a networked build the fonts are
   downloaded once and embedded in the build output — no runtime font requests,
   so the offline-first guarantee is unaffected.

`frontend/next.config.ts` gained an env-gated, dev-only `allowedDevOrigins`
(`NEXT_DEV_ALLOWED_ORIGINS`) so tunnel/preview hostnames can load `/_next/*`
assets; production builds ignore it.

Preview routes verified as HTTP 200 with `<html class="dark">`: `/`, `/sign-in`,
`/sign-up`, `/blog`, `/changelog`, `/demo`, plus `/api/auth/get-session` and the
brand image (byte-identical, 1,057,282 bytes). The library/editor require
PostgreSQL and the backend stack, so they are reachable only through
`docker compose up`.

Exact commands to finish verification on a Windows machine:

```powershell
docker compose up -d --build
docker compose ps
curl http://localhost:8000/local-ai/status
curl http://localhost:3107/api/local-ai/status        # behind a session cookie
cd frontend; corepack pnpm exec prisma generate; corepack pnpm build
corepack pnpm exec playwright install chromium; corepack pnpm test:e2e
```

Internet-blocked workflow test: **not executed** (no Docker/ffmpeg/Ollama in the
sandbox). The code-level guarantee is enforced and unit-tested — hosted
providers are refused with `OFFLINE_MODE=true`, model downloads only happen via
the explicit setup scripts, and the worker preflight fails fast — but the
end-to-end run with an outbound firewall still needs to be performed on the
target machine, e.g. with Windows Defender Firewall outbound rules set to block
all traffic except loopback.

---

## 5. Internet-dependent features (unchanged, now labelled)

| Feature | Needs internet | Offline behaviour |
|---|---|---|
| YouTube / Twitch / Kick downloads | yes | clear error; local files unaffected |
| Pexels stock B-roll | yes | no new B-roll; existing placements still render |
| Social publishing & analytics | yes | disabled/labelled; manual upload remains |
| Hosted AI (OpenAI/Anthropic/Google/OpenRouter/AssemblyAI) | yes | refused unless `OFFLINE_MODE=false` **and** `ALLOW_HOSTED_AI=true` |
| DataFast analytics, SES billing emails, feedback forwarding | yes | optional; no effect on local workflow |

The app itself never blocks the local workflow behind any of these.

---

## 6. Performance limits on Ryzen 5 4600U / 16 GB / no GPU

* `small` on 6 cores (CPU float32) is roughly 0.3–0.6× real time: a 20-minute
  source typically needs 35–70 minutes for transcription alone. `base` is about
  twice as fast (≈142 MB checkpoint, lower accuracy on accents/noise).
* `llama3.2:3b` needs ~2.5–3.5 GB while answering; analysis on a 60-minute
  transcript takes a few minutes across windows plus a rerank pass.
* Rendering a 60-second 1080×1920 clip with captions takes about 20–60 s.
* Keep `medium`/`large` off this machine; run transcription and rendering
  sequentially rather than in parallel to avoid memory pressure.

---

## 7. Confirmations

- **Rebrand:** every user-visible surface says ClipWright Studio — title, wordmarks, landing, auth, dashboard, sidebar, editor, settings, dialogs, toasts, export screens, metadata, manifest, docs and error strings. `SupoClip` remains only in licence/attribution, upstream URLs, blog slugs and documented wire identifiers (`x-supoclip-*`, queue names, DB role, `supoclip_mcp`).
- **Logo:** the supplied `ClipWright Icon.png` is the sole artwork; it is stored once as `frontend/public/brand/clipwright-logo.png` (byte-identical, transparent, unmodified) and every icon/favicon/PWA size is derived from it. No SupoClip logo remains.
- **Dark theme:** dark is the default on landing, auth, app shell, library, editor, timeline, caption editor, settings, dialogs, toasts, tables and tooltips; it persists across restart (`clipwright-theme`), and a light theme is opt-in.
- **Licence:** AGPL-3.0 `LICENSE` and upstream attribution preserved; `docs/REBRANDING.md` documents every rename decision.

---

## 8. Known gaps / next actions

1. `pnpm build`, Playwright e2e, `docker compose up`, the Ollama pull and the
   Whisper download could not be executed here (blocked network/TLS to
   `binaries.prisma.sh`, no container runtime).
2. The offline firewall test must be run on the Windows machine.
3. A handful of hosted-mode translation keys (`marketing.appStore*`,
   `footer.ios`, admin App Store-offer-code copy) are unused but kept so locale
   key parity and the preserved hosted/creator features stay intact.
4. iOS/App-Store-specific product surfaces are gone from the app; the
   `supoclip-ios/` client token parser is kept for compatibility.
