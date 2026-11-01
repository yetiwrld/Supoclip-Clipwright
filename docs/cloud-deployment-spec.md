# Cloud deployment specification — v2 (Oracle VM + local disk + Tailscale)

**Status:** REVISED SPEC + READ-ONLY AUDIT. No repository code changed, no data migrated, no cloud resource created.
**Supersedes:** v1 of this document (commit `e7e2740`), which targeted OCI Object Storage + Neon. That design is deferred (§12), not deleted — the measured S3-compatibility findings are preserved there for the off-site-backup path.
**Branch:** `arena/dd48529f-supoclip-clipwright` @ `e7e2740` · **Date:** 2026-11-01.

Evidence markers: **[repo]** read from this working tree · **[docs]** official vendor docs (linked) · **[test]** executed in the audit sandbox · **[your machine]** cannot be verified from here — exact command given.

---

## 1. Correction log — what I got wrong in v1

| My v1 claim | Verdict | Correct statement |
|---|---|---|
| "Moving processing off the laptop makes it faster" | **wrong** | It makes the *laptop* free. 2 OCPU (4 vCPU-equivalent threads) at ~2.4–3 GHz ARM is not faster than a Ryzen 5 4600U at 6C/12T for x264 or Whisper. Expect equal-to-slower until measured. |
| "The only trade-off is durability" | **wrong** | There are three: durability (single block volume), **idle reclamation** of the whole VM (§5, Gate 5), and no isolation from a noisy co-tenant's CPU. |
| "40–60 videos at 150 GB" | **unverified** | Depends on source bitrate, the capped-master re-encode, renders and scratch. §6 replaces the number with a formula and a measurement you run once. |
| "triton blocks arm64 for Whisper" (v1 §4.5) | **outdated** | In the pinned lock, `triton 3.6.0` ships 6 aarch64 wheels, and Whisper's dependency marker is `platform_machine == 'x86_64' and sys_platform == 'linux'` — so it is not installed on arm64 at all. **[repo]** Gate 1 is clean. |

---

## 2. Architecture (locked)

```
 WINDOWS 11 LAPTOP — UI only, runs NATIVELY (no Docker for the frontend)
   node .next/standalone/server.js   → http://localhost:3107
   Better Auth + Prisma  ─────────────┐
                                      │ Tailscale (private, WireGuard)
 ─────────────────────────────────────┼───────────────────────────────────────
 OCI A1 VM  ·  Ubuntu 24.04 arm64  ·  2 OCPU / 12 GB  ·  200 GB block total
   boot 47–50 GB  ·  ~150 GB for data
   docker compose (cloud file):
     backend   127.0.0.1:8000 → published on the tailnet only via `tailscale serve`
     worker    arq supoclip_tasks      (concurrency 1)
     ai-worker arq supoclip_ai         (concurrency ≤2, Whisper + Ollama)
     ollama    internal :11434, models volume
     postgres  internal, no published port
     redis     internal, no published port
   media root on the data volume:  uploads/ clips/ models/ config/  (existing layout)
```

**Why the frontend runs natively:** see Gate 2. The Docker path is not just slower to configure — on Windows it is likely *unable* to reach the tailnet at all.

---

## 3. The six gates

| # | Gate | Status here | Blocking? |
|---|---|---|---|
| 1 | ARM64 compatibility | **[repo]** clean, 3 residual risks | needs a build on the VM to close |
| 2 | Tailscale reachability from the local frontend | **[repo]** design flaw confirmed | **yes** — decides native vs Docker |
| 3 | Upload path without presigned S3 | **[repo]** solved by existing code + config | no, but CORS/origin must be right |
| 4 | Disk monitoring | **[repo]** gaps confirmed | yes — needs a small code addition |
| 5 | Idle-instance reclamation | **[docs]** criteria confirmed; risk real | policy decision, not code |
| 6 | Independent backup/restore | absent today | **yes** — must exist before cutover |
| 7 | Runtime settings split (found during this audit) | **[repo]** new finding | yes, small |

---

### Gate 1 — ARM64 compatibility

**Verified [repo]:**

* All 220 locked packages resolve on arm64. Only sdists: `openai-whisper` (pure Python), `srt` (pure Python), the legacy `whisper` shim, and the project itself. `pywin32` is Windows-only by marker.
* `torch 2.11.0+cpu` has 6 aarch64 wheels; `numba`, `llvmlite`, `opencv-python`, `numpy`, `triton` all have aarch64 coverage.
* **No hardware-encoder assumptions anywhere.** Every encode path is `libx264` (`src/clip_editor.py`, `src/media/ffmpeg.py`, `src/video_utils.py`, `src/editor_document.py`, `src/services/editor_service.py`). No NVENC/QSV/AMF/VideoToolbox, no `hwaccel` flags. So arm64 is not a codec problem — it is only a *speed* problem.
* Base images are all multi-arch: `python:3.11.17-slim-trixie`, `node:22.23.4-bookworm-slim`, `redis:7-alpine`, `postgres:15-alpine`, `ollama/ollama` (arm64 published). `bgutil`, `vector`, `mcp` are not used in cloud mode.
* apt packages (`ffmpeg`, `tesseract-ocr`, `fonts-noto-*`) all exist on Debian trixie arm64.

**Residual risks to close on the VM:**

1. **`cv2` at import time** — five media modules `import cv2` (captions, common, face_detection, reaction_layout, gap_activity) and the Dockerfile installs no `libgl1`/`libglib2.0-0`. If the import fails, add both packages.
2. **Duplicate Whisper packages** — `pyproject` depends on both `openai-whisper` **and** the legacy `whisper 1.1.10` (both sdist-only, both provide a `whisper` module). Whichever resolves first on `sys.path` wins. Pre-existing behaviour, but on a fresh arm64 build it determines *which* implementation transcribes. Verify with `python -c "import whisper; print(whisper.__file__, whisper.__version__)"` — must be `openai-whisper`.
3. **Deno downloaded during image build** (`curl … deno.land/install.sh`) — build-time internet only; aarch64 binary exists. Not a runtime dependency unless YouTube downloads are used.

**Build must happen on the VM** (`BACKEND_PLATFORM=linux/arm64`, build arg `BACKEND_CPU_ONLY=true`), never cross-built on the laptop.

**Pass criteria [your machine]:**
```
uname -m                                   → aarch64
docker compose … exec backend python -c "import torch,platform;print(platform.machine(),torch.__version__,torch.get_num_threads(),torch.cuda.is_available())"
                                            → aarch64, +cpu, 2, False
docker compose … exec backend python -c "import whisper;print(whisper.__file__)"     → openai-whisper
docker compose … exec backend python -c "import cv2;print(cv2.__version__)"
docker compose … exec backend ffmpeg -hide_banner -encoders | grep -c libx264        → 1
```

---

### Gate 2 — Tailscale from the local frontend

**Verified [repo]:**

* The local frontend's API routes reach the backend server-side via `BACKEND_INTERNAL_URL`, which the current compose sets to `http://backend:8000` — Docker-internal DNS that exists only inside the compose network.
* The frontend's production stage exists (`frontend/Dockerfile` runner stage: `NODE_ENV=production`, `node server.js`, port 3107); the compose file uses the **development** stage (`pnpm run dev`).

**Assessment [your machine]:** a container on Docker Desktop for Windows (WSL2 backend) does not inherit the Windows host's Tailscale routes or MagicDNS. Installing Tailscale on Windows does not let `backend`-style hostnames or `100.x.y.z` addresses resolve inside a container.

**Recommendation:** run the frontend **natively on Windows** (`PORT=3107 node .next/standalone/server.js`). Two reasons beyond routing: `NEXT_PUBLIC_*` values are inlined at build time, so a native production build is the only way to be sure what the browser bundle contains; and Node on the host can reach the tailnet directly.

**Decide with this test before anything else [your machine]:**
```powershell
docker run --rm curlimages/curl -sS -m 5 http://100.x.y.z:8000/health   # 100.x = VM tailnet IP
```
* Fails → native frontend (recommended path).
* Succeeds → Docker frontend also viable, but must set `BACKEND_INTERNAL_URL` to the tailnet URL and *not* `backend:8000`.

**Note:** the backend binds `127.0.0.1:8000` inside the VM, so the tailnet URL only exists after `tailscale serve` publishes it (Gate 3). Do not open port 8000 on the public interface to work around this.

---

### Gate 3 — Upload path without presigned S3

**Verified [repo] — this needs no new code.** With `MEDIA_STORAGE=local`, `POST /upload/authorize` returns `{"storage": "local"}`; the Next route then answers the browser with:

```
{ directUpload: false, url: `${NEXT_PUBLIC_API_URL}/upload`, headers: <signed backend auth headers> }
```

The browser POSTs the file **directly** to that URL. The backend accepts it via `_read_limited_upload_form`: a single `Content-Length` is mandatory, the body is streamed (never buffered whole), and it is rejected with 413 above `MAX_VIDEO_UPLOAD_BYTES` before the body is read.

So direct browser→VM upload works today *if*:
1. `NEXT_PUBLIC_API_URL` is a URL the browser can reach (i.e. the tailnet HTTPS name), **set at build time**;
2. `CORS_ORIGINS` on the backend contains `http://localhost:3107`;
3. `MAX_VIDEO_UPLOAD_BYTES` is sane for the disk (§6).

**Two consequences to budget for:**

* **Transient 2× disk per upload.** Starlette spools the multipart body to `TEMP_DIR=/app/uploads` (same volume as the sources), then it is copied to the final path. A 2 GB upload peaks at ~4 GB.
* **Resumability is absent.** One POST, one shot. A dropped connection restarts the upload. The v1 object-storage design had resumable parts; this one does not. Acceptable on a home uplink for <2 GB files; state it plainly rather than discover it.

**Pass criteria [your machine]:** upload a 1.5 GB file through the UI; in DevTools the request must go to the VM's tailnet host (not `localhost`), never exceed one in-flight body, and the backend returns 200 with a `video_path`; then confirm `du -h` on the VM shows the file once, not twice.

---

### Gate 4 — Disk monitoring

**Verified [repo] — the gaps are real.** Free-space knowledge exists only in three narrow places:

| Location | What it guards |
|---|---|
| `src/media/source_limits.py:366` | the capped-master re-encode (needs size + 32 MB) |
| `src/media_replication.py:89,348,412` | the replication outbox (unused in this design) |
| `src/rapidapi_youtube_downloader.py:228` | YouTube download (needs 2×) |

Nothing guards: the upload itself, clip rendering, export, Ollama model pulls, Docker image growth, or log growth. There is no endpoint that reports free space, so the UI cannot warn you. The fresh finding: **`TEMP_DIR=/app/uploads` shares a volume with sources**, so ffmpeg scratch competes with the media library.

**What I would add (small, testable):** a `DISK_FREE_MIN_BYTES` setting checked before upload authorization, before enqueueing a render, and before an Ollama pull; exposed through the existing `/local-ai/status` readiness payload so the UI can show "42 GB free — low" instead of failing mid-render.

**Measurement you run once [your machine]:**
```bash
df -h /                       # boot volume
df -h /srv/clipwright         # data volume
docker system df              # images + build cache
sudo du -sh /srv/clipwright/* | sort -h
du -sh /var/lib/docker/volumes/* | sort -h   # redis/pg/ollama
```

---

### Gate 5 — Idle reclamation

**Confirmed [docs], Oracle "Always Free Resources" (updated 2026-06-12):** an instance is idle if, over a 7-day period, **CPU 95th-percentile < 20%, network < 20%, and memory < 20%** (memory applies to A1 only).

**Honest estimate for this workload at rest:** Postgres ~0.3 GB, Redis ~30 MB, backend ~0.4 GB, both workers ~0.4 GB idle, OS ~0.4 GB → **~1.5 GB of 12 GB ≈ 12 %**, CPU ~1–3 %, network negligible. All three criteria would be met on a quiet week, so **reclaim is a genuine risk**, not a formality.

**Options (choose one, D3):**

1. **Treat the VM as replaceable (recommended).** Keep a provisioning script + off-box backups (§Gate 6). A reclaim then costs an hour, not your work. Protects against every other failure mode too (disk corruption, account issues, region changes).
2. **Keep memory above the line deliberately** — load the Ollama model at boot with `OLLAMA_KEEP_ALIVE=-1` so ~2.5 GB stays resident (~20 %). Effective, but it is gaming a metric; a model reload after a reboot could put you back under.
3. **Move to Pay As You Go** — reclamation no longer applies, Always Free limits still don't bill, but any accidental overage becomes an invoice. This reverses your "no surprise charges" constraint.

My recommendation is 1, with 2 as a harmless side effect if Ollama happens to keep a model warm.

---

### Gate 6 — Independent backup and restore

Five volume backups per volume is a safety net inside one tenancy. It is not an independent copy: same provider, same account, same region, same failure domain.

**Design (all free):**

| Piece | What | Where | Retention |
|---|---|---|---|
| Database | `pg_dump` compressed, per run | VM data volume **and** pulled to the laptop over Tailscale | 7 daily + 4 weekly |
| Media + projects | `rsync -a --delete` of `uploads/`, `clips/`, `config/` | laptop external drive (and optionally R2's 10 GB free tier for the most recent project) | 2 generations |
| Restore drill | restore dump into a scratch database, point `DATABASE_URL` at it, open a project | — | **run once before cutover** |

**Pass criteria:** the restore drill opens a project and plays one clip *from the restored copy only*, with the original files untouched.

---

### Gate 7 (new) — Runtime settings live in a shared file

**Found [repo]:** both sides read the same runtime switch from `<CLIPWRIGHT_CONFIG_DIR>/offline-mode.json` — the backend writes it (`src/offline_mode.py`), the frontend reads it (`src/lib/offline-mode.ts`), and compose mounts the laptop's `data/config` into the frontend as `:ro`.

Split across two machines, that file exists twice: toggling offline mode in the UI writes to the VM's copy while the laptop's Next keeps reading its own. Small, but it produces exactly the "the setting reverted" class of bug you asked me to eliminate.

**Options:** mirror the file with the same rsync used for backups (simplest), or have the frontend read the state from the backend status endpoint instead of the file (cleaner, a few lines, needs a test). I recommend the second.

---

## 4. Revised processing plan (replacing the "it will be faster" framing)

| Setting | Current default | Cloud value | Reason |
|---|---|---|---|
| worker `max_jobs` | 4 (literal) | **1** | four concurrent ffmpeg jobs on 2 cores thrash and OOM 12 GB |
| ai-worker `max_jobs` | 16 (literal) | **2** | Whisper + Ollama both want cores |
| `WHISPER_MODEL_SIZE` | `small` | **`base`** initially | 3.4× fewer parameters; measure before spending on `small` |
| `AI_EDIT_TIMEOUT_SECONDS` | 120 | 600 | `llama3.2:3b` on 2 ARM cores |
| `MEDIA_UPLOAD_CONCURRENCY` | 3 | 1 | 2× transient disk per upload |
| `MAX_VIDEO_DURATION` | 5400 s | unchanged | keep behaviour identical |

Both concurrency values are hard-coded today and must become env-driven (`WORKER_MAX_JOBS`, `AI_WORKER_MAX_JOBS`) with the existing defaults preserved so nothing regresses locally.

Benchmark before any claim: time one 30-minute source end-to-end on the VM (transcribe → select → render → export) and record it next to the same job on the laptop. That number, not an estimate, goes in the report.

---

## 5. Storage worksheet (replaces the "40–60 videos" claim)

```
U = uploads volume      = Σ source sizes + Σ capped masters (transient, ≤1× source) + 2 × largest in-flight upload
C = clips volume        = Σ renders (short, but many per source)
M = models volume       = whisper base 0.14 GB / small 0.46 GB / large 2.9 GB + ollama llama3.2:3b ≈ 2 GB
D = docker              = ~10–14 GB (torch + ffmpeg + opencv layers), plus build cache
P = postgres + redis    = DB size + AOF tail (formula in §6 of v1)
F = free floor          = DISK_FREE_MIN_BYTES, keep ≥ 20 GB
usable ≈ 150 GB − D − M − P − F   →  realistically ~110–120 GB for U + C
```

Two multipliers people miss, both verified here: every upload needs **2×** transiently (Gate 3), and a source above 1080p/60 fps is re-encoded into a capped master **while the original still exists** (`source_limits._enough_disk`). A 4K source can therefore briefly cost ~3× its own size.

Run the measurement and the number follows; do not plan against an estimate.

---

## 6. Work items after approval (no S3 code changes in this phase)

| # | Item | Files | Notes |
|---|---|---|---|
| 1 | Cloud compose file (additive) | `docker-compose.cloud.yml` **(new)** | no frontend/mcp/bgutil/vector; arm64; **no published ports** for postgres/redis/ollama; log rotation; memory limits |
| 2 | Env-driven worker concurrency | `backend/src/config.py`, `workers/tasks.py`, `workers/ai_worker.py`, tests | defaults unchanged locally |
| 3 | Disk guard + status surface | `backend/src/config.py`, upload route, render entry points, `/local-ai/status` | Gate 4 |
| 4 | Cloud env template | `.env.cloud.example` **(new)** | tailnet URLs, CORS, storage root, quota — no secrets |
| 5 | Offline-mode state made backend-authoritative | `frontend/src/lib/offline-mode.ts` + test | Gate 7 |
| 6 | Backup/restore scripts | `scripts/cloud-backup.sh`, `scripts/cloud-restore.sh` **(new)** | Gate 6; restore drill documented |
| 7 | Runbook | `docs/cloud-deployment.md` **(new)** | provision → verify → acceptance → cutover → rollback |
| 8 | Tests | disk guard, concurrency env, quota, compose shape | run with the existing suites, same baseline |

Explicitly **not** in this phase: `media_storage.py` changes, object storage, Neon, Lambda, any DB schema change.

---

## 7. Cutover acceptance gate (your "local stays untouched" rule)

1. Local installation keeps running and stays authoritative throughout.
2. Cloud stack is a **second, separate installation** (its own database and media root). No shared state, no migration yet.
3. Acceptance test on the cloud install, in this order: import → transcribe → AI select → edit → render/export → save → restart everything → reopen the project → play the clip. Record the timings and the free-space delta.
4. Only after that passes: migrate data (copy, verify, keep originals — the same copy-then-release discipline as before), then cut the laptop's `BACKEND_INTERNAL_URL` over.
5. Rollback at any time = point the laptop back at the local stack. Nothing is deleted until you say so.

---

## 8. Decisions I need

* **D1 — Frontend runtime:** native on Windows (**recommended**, pending the Gate 2 test) or keep Docker and reconfigure its networking?
* **D2 — Idle-reclaim posture:** replaceable-VM + backups (**recommended**), keep-alive trick, or Pay As You Go?
* **D3 — Whisper size on the VM:** `base` first (**recommended**) or straight to `small`?
* **D4 — Upload ceiling:** 2 GB per file (**recommended**, keeps 2× transient headroom) or keep 4 GB?
* **D5 — Region:** which home region (compute and volumes must be in it)?
* **D6 — Start work:** the eight items in §6, or only items 2–4 (concurrency, disk guard, env) to keep the first change small?

---

## 9. Deferred: the object-storage option (condensed from v1)

Kept because off-site backup may want it later. Verified findings that still stand:

* OCI Always Free: **20 GB** object storage total, **50,000 requests/month**; S3 API covers PutObject/GetObject/Head/Delete/multipart/ListParts but **not** PutBucketCors, lifecycle, or bucket policies (configure those with the OCI CLI), and lists only `ListObjects`, not `ListObjectsV2` — the repo uses `list_objects_v2` in six places.
* boto3 1.43 presigns `upload_part` with `content-length` in the signed headers **[test]**, and defaults to `when_supported` checksums which this repo's primary client does not override.
* Cloudflare R2 (better-behaved S3: 10 GB, 1M Class A / 10M Class B ops, zero egress) is the sensible target if a small off-site copy is ever wanted; note `MEDIA_FALLBACK_*` mirrors every object, so it must not be enabled as a "replica" on a capped tier.
* Neon remains unsuitable for the current cron schedule (~182 CU-hours/month against a 100 allowance).

---

## 10. What I did and did not do

* **Did:** read the repo, parsed the lockfile, inspected the upload/auth/config paths, read Oracle's free-tier and S3-compatibility documentation.
* **Did not:** change any application code, config, `.env`, schema, or container; create any cloud resource; move or delete any data; run any migration.
* This document is the only artefact. v1 remains in git history at `e7e2740`.
