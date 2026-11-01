# ClipWright Studio — zero-cost cloud migration plan

**Status:** AUDIT COMPLETE — PLAN ONLY. Nothing was created, changed, deployed, or deleted while producing this document.
**Branch:** `arena/dd48529f-supoclip-clipwright` (HEAD `2d26b05`).
**Date:** 2026-11-01.

> Read-only constraints honoured: no cloud accounts/CLI/MCP calls, no resources, no `.env` edits, no database changes, no container stops, no media uploads. Every claim below is marked with how it was established:
> **[repo]** = read from this working tree · **[docs]** = official vendor documentation fetched today · **[test]** = script executed locally in the audit sandbox · **[assumption]** = needs a real tenancy to confirm.

---

## 0. Executive summary

The migration is feasible for **$0**, but there are **three hard limits** that decide the design, and the "obvious" architecture fails on all three if taken literally:

| Limit | Number | Consequence |
|---|---|---|
| Oracle Always Free Arm A1 is **1,500 OCPU-hours + 9,000 GB-hours/month** = **2 OCPU / 12 GB** running continuously | **[docs]** (oracle.com/cloud/free, fetched today) | The machine is **2 vCPU / 12 GB**, not the 4/24 that most guides quote. Sizing it at 4/24 leaves Always Free and is billable after the 30-day credit. |
| Oracle Always Free object storage is **20 GB total** (standard+IA+archive) and **50,000 API requests/month** | **[docs]** | The bucket is small. A 4 GB-per-file cap means ~4–5 videos fill it. Needs an application quota and a smaller per-file cap. |
| Neon Free is **100 CU-hours + 5 GB transfer + 1 GB storage per project**, and **suspends compute when either metre runs out** | **[docs]** (neon.com/pricing) | The current worker crons query Postgres every **20 s, 40 s and 5 min**, forever **[repo]** → compute never scales to zero → ~182 CU-hours/month > 100 → **the database suspends mid-month**. Neon cannot host this app unmodified. |

**Recommended architecture (all free, no billing surface):**

```
 Windows 11 laptop — UI only
   http://localhost:3107  Next.js standalone server (`node .next/standalone/server.js`)
   • serves the UI, auth screens, API proxy
   • Better Auth/Prisma ──┐
                          │ private (Tailscale)
 ─────────────────────────┼──────────────────────────────────────────────
 OCI A1 VM, Ubuntu 24.04 arm64, 2 OCPU / 12 GB, ~150 GB free block space
   docker compose:
     backend     :8000   uvicorn          (tailnet-only)
     worker      arq  supoclip_tasks
     ai-worker   arq  supoclip_ai        ← Whisper + Ollama here
     redis       internal only
     postgres    internal only (laptop Prisma via SSH tunnel / tailscale0)
     ollama      internal only (:11434, models in a named volume)
   media working set on block volume (150 GB)
                          │
                          ▼
 OCI Object Storage bucket (private, S3-compatible, 20 GB free)
   • browser PUTs multipart parts directly (no laptop disk, no VM pass-through)
   • workers GET/PUT over the private VCN / internet
   • playback + export = browser 307 → presigned URL (never through Next)
```

**Why not the alternatives:**

* **AWS Lambda** — the backend imports `torch`, `whisper`, `opencv`, `ffmpeg` bindings. Lambda's package limit is **<10 MB** zipped (250 MB unzipped). Moving the API to Lambda without rebuilding it as a thin client is a rewrite; and `ReplicatedMediaClient` needs a shared POSIX filesystem with `flock` **[repo]**, which Lambda cannot provide. AWS is therefore **not** part of the free stack.
* **Neon Postgres** — see the third limit above. Viable only with code changes to gate the always-on crons. Kept as a documented fallback in §8.
* **Cloudflare R2** — 10 GB free, but OCI's 20 GB is larger, and `MEDIA_FALLBACK_*` **duplicates every object** into a second bucket **[repo]**, so enabling both costs 2× storage and gets you nothing on the free tier.

Six decisions need your sign-off before I write code — see §18 (D1–D6).

---

## 1. What the app actually does over the network today (so we know what moves)

| Flow | Today **[repo]** | After migration |
|---|---|---|
| **Upload** | `POST /api/upload/authorization` (Next) → backend `POST /upload/authorize`. If `MEDIA_STORAGE=s3` the backend returns three presigned multipart PUTs and the **browser uploads 64 MB parts straight to S3**; otherwise it returns `{"storage":"local"}` and the browser streams the whole file to `POST ${NEXT_PUBLIC_API_URL}/upload` (`maxDuration 3600`, requires `Content-Length`, cap `NEXT_PUBLIC_MAX_VIDEO_UPLOAD_BYTES`, default **4 GB**). | Unchanged code, bucket = OCI. **`MEDIA_STORAGE=s3` is mandatory**, otherwise the fallback streams the file through the laptop — exactly what you don't want. |
| **Playback / clip download** | `backend/src/api/routes/media.py` returns **307 → `MediaStorage().signed_url(...)`** when S3 is on, else `FileResponse` from the laptop. | 307 → OCI presigned GET. **No video bytes transit the laptop.** Bucket egress is free to 10 TB/month **[docs]**. |
| **Processing** | worker/ai-worker read the source from the media volume (or download from S3 into a local cache with `flock` leases). | Workers download into the VM's 150 GB working set; cache leases still avoid repeat downloads. |
| **Metadata** | Postgres (`postgresql+asyncpg://…`) for backend/workers; Prisma/`DATABASE_URL` for the frontend; **two independent migration runners** (backend raw SQL + `init.sql`, and 27 Prisma migrations). | One Postgres on the VM. Both runners must be executed once. |
| **Queue** | ARQ queues `supoclip_tasks`, `supoclip_fast`, `supoclip_ai` on Redis; 7 cron jobs. | Redis on the VM, not published. |
| **LLM** | Ollama over HTTP (`OLLAMA_BASE_URL`, `OLLAMA_API_KEY`), plus transcription. | Ollama container on the VM; nothing published to the internet. |

---

## 2. Verified free-tier envelope

### Oracle Cloud Always Free **[docs — oracle.com/cloud/free, fetched 2026-11-01]**

| Resource | Always Free amount | Notes for us |
|---|---|---|
| Arm Ampere A1 | **1,500 OCPU-hours + 9,000 GB-hours / month**, "12 GB of memory usable as 1 VM or 2 VMs" | ≈ **2 OCPU / 12 GB** running 24/7. Capacity is not guaranteed at create time ("out of capacity" is common); pick a region that has room. |
| AMD micro | 2 × VM.Standard.E2.1.Micro (1/8 OCPU, 1 GB) | Too small for ffmpeg/Whisper; ignore. |
| Block volume | **200 GB total**, up to 2 volumes, 5 backups | Boot 47–100 GB leaves ~100–150 GB for the media working set. |
| **Object Storage** | **20 GB total for standard + infrequent + archive** | The binding constraint on media. |
| **Object Storage requests** | **50,000 / month** | See quota maths in §6. |
| Outbound transfer | 10 TB / month | Streaming clips to you is free. |
| Load balancer | 1 × 10 Mbps flexible | Not needed if we use Tailscale. |
| Trial credit | US$300 / 30 days | **Ignore it — it expires.** Everything above must fit Always Free after day 30. |
| Account rules | 1 free account per person; card required at signup (verification only); **accounts idle 30+ days may be suspended** | The worker crons keep the tenancy non-idle, but keep an eye on this. |
| Not free | GPU, Windows images, >20 GB object storage, >200 GB block | Do not touch. |

### Neon **[docs — neon.com/pricing, fetched 2026-11-01]**

| Item | Free plan |
|---|---|
| Compute | **100 CU-hours / month per project**, default size 0.25 CU, max 2 CU |
| Scale to zero | after 5 min idle; wakes in a few hundred ms |
| Storage | **1 GB per project**, 20 GB across projects; writes fail above it |
| **Network transfer** | **5 GB / month** |
| On limit | **compute is suspended until the next billing period** (no charge, but the app breaks) |
| Card | not required |

---

## 3. Component mapping

| Component | Where it runs | Key settings |
|---|---|---|
| `frontend` (Next.js) | **Laptop**, production build. `next.config.ts` already sets `output: "standalone"` **[repo]**. | `BACKEND_INTERNAL_URL`=tailnet backend URL · `NEXT_PUBLIC_API_URL`=same · `BETTER_AUTH_URL`/`NEXT_PUBLIC_APP_URL`=`http://localhost:3107` · `DATABASE_URL`=VM Postgres over tailnet · `MEDIA_*=s3`? **no** — the frontend never needs S3 creds, only the backend does |
| `backend` | OCI VM, container, `linux/arm64` (compose build arg `BACKEND_CPU_ONLY=true` + `BACKEND_PLATFORM=linux/arm64` **[repo]**) | `MEDIA_STORAGE=s3`, `CORS_ORIGINS`=frontend origin, `DATABASE_URL`, `REDIS_URL`, `OLLAMA_BASE_URL=http://ollama:11434`. Stays bound to `127.0.0.1:8000`; `tailscale serve` publishes it on the tailnet only. |
| `worker` | OCI VM | same env, `max_jobs` 4 → **1** (§14) |
| `ai-worker` | OCI VM | `WHISPER_MODEL_SIZE=base` **[assumption — measure]**, `OLLAMA_BASE_URL=http://ollama:11434`, `max_jobs` 16 → **2** |
| `redis` | OCI VM, **not published** | `redis:7-alpine`, AOF |
| `postgres` | OCI VM, **no published port** | `postgres:15-alpine`, `./init.sql`. The laptop reaches it through an SSH tunnel (`ssh -N -L 5432:localhost:5432 …`) or a port bound to the `tailscale0` address. |
| `ollama` | OCI VM, **not published** (compose profile `ollama`) | models volume, `llama3.2:3b` |
| Object storage | OCI bucket | `MEDIA_S3_ENDPOINT=https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, `MEDIA_S3_REGION=<region-id>`, customer secret key pair |
| `mcp`, `bgutil`, `vector` | **disabled** | profiles only; `bgutil` is just for YouTube downloads |

---

## 4. S3-compatibility audit of this codebase against OCI

The code is written against real AWS S3 through boto3. I verified what OCI's S3 Compatibility API supports and what this code calls.

### 4.1 Supported by OCI **[docs — Object Storage Amazon S3 Compatibility API Support, updated 2026-10-19]**

Bucket: `PutBucket`, `HeadBucket`, `GetLocation`, `ListObjects`, `DeleteBucket`
Object: `PutObject`, `GetObject`, `HeadObject`, `DeleteObject`, `BulkDelete`, `RestoreObjects`
Multipart: `InitiateMultipartUpload`, `UploadPart`, `ListParts`, `ListUploads`, `CompleteMultipartUpload`, `AbortMultipartUpload`
Tagging: bucket tagging only. SigV4 only (SigV2 unsupported). Path-style **and** virtual-hosted endpoints both work. `aws-chunked` uploads supported, with trailer checksum validation for CRC32/CRC32C/SHA256/CRC64NVME.

### 4.2 **Not** listed as supported — and used or needed by us

| Operation | Where we use it **[repo]** | Impact |
|---|---|---|
| `ListObjectsV2` (only `ListObjects` is listed) | `media_storage.py:611,724`, `workers/media_cleanup.py:203,209,227`, `media_replication.py` paginator shim | Prefix listings (editor restore, media sweeps) may 501. **Must test**; fallback = switch those call sites to the v1 paginator. |
| `PutBucketCors` / `GetBucketCors` | needed for browser→bucket uploads | Must be configured with the **OCI CLI/native API**, not boto3. |
| `PutBucketLifecycleConfiguration` | needed to abort dangling multipart uploads | Same — OCI CLI lifecycle policy. |
| `PutBucketPolicy` / `PutBucketEncryption` / `PutPublicAccessBlock` | `scripts/setup-media-storage.py` uses AWS-style bucket policy + SSE + TLS-only policy | **That script will not work against OCI.** A new OCI setup script is required (§14). |
| Object ACLs | OCI has no object ACLs — IAM policies only | Fine: the bucket stays private, access is via presigned URLs. |

### 4.3 Empirically verified boto3 behaviour **[test]**

I installed the exact pinned version (`boto3 1.43.2` / `botocore 1.43.123`) in the audit sandbox and generated the same URLs `media_storage.py` generates, against an OCI-style endpoint:

```
client = boto3.client("s3", region_name="eu-frankfurt-1",
                      endpoint_url="https://nsxyz.compat.objectstorage.eu-frankfurt-1.oraclecloud.com",
                      config=BotoConfig(signature_version="s3v4",
                                        retries={"mode":"standard","max_attempts":4}))

upload_part (with ContentLength)  host: nsxyz.compat.objectstorage.eu-frankfurt-1.oraclecloud.com
                                  path: /clipwright-media/media/sources/abc/video.mp4
                                  X-Amz-SignedHeaders: content-length;host
get_object  (with ResponseContentDisposition)  X-Amz-SignedHeaders: host

request_checksum_calculation:  when_supported
response_checksum_validation:  when_supported
```

Three consequences:

1. **Path-style addressing is already what boto3 does for a custom endpoint** — no config change strictly needed, but I recommend pinning `s3={"addressing_style": "path"}` so a botocore upgrade can't silently switch to virtual-hosted style (which needs the `vhcompat…` endpoint form).
2. **The multipart part URL signs `content-length`.** The browser must send exactly the signed length — it does, because the part is a `Blob.slice()` whose size is what the backend signed. **But if OCI does not byte-compare the `Content-Length` header in its canonical request, every part upload returns `403 SignatureDoesNotMatch`.** This is the single highest-risk unknown. Fallback if it fails: stop signing `ContentLength` for parts and validate the size server-side in `complete_upload` via `ListParts`. **Must be tested in the tenancy before any migration.**
3. **boto3 1.43 defaults to `request_checksum_calculation="when_supported"`**, so it will attach `x-amz-checksum-crc32` + `x-amz-sdk-checksum-algorithm` to `PutObject`/`UploadPart`. The repo already opts out **only for the fallback client** (`media_storage.py:144`) — the **primary** client (`media_storage.py:115-137`) has no such setting, and also no `connect_timeout`/`read_timeout` (botocore default 60 s each). Fix: pass `request_checksum_calculation="when_required"`, `response_checksum_validation="when_required"`, and explicit timeouts to the **primary** client. The OCI `aws-chunked` + trailer-checksum support suggests this may work either way, but "when_required" removes the variable.

### 4.4 Two additional storage facts that shape the plan **[repo]**

* `MEDIA_FALLBACK_*` wires up `ReplicatedMediaClient`, which **mirrors every write to a second bucket** and keeps a durable outbox in `config.temp_dir`. On a 20 GB free tier this halves your usable space. **Leave the fallback unset** (`MEDIA_FAILOVER_ENABLED=false`).
* That outbox uses `fcntl.flock` and a shared directory, so **backend and both workers must run on one host** with one shared volume. That is exactly what the single VM gives us — and it is another reason Lambda/serverless engines are out.
* `MEDIA_S3_FORCE_PATH_STYLE` does not exist yet; `MEDIA_S3_REGION` is only used as the SigV4 signing region (must equal the OCI region identifier, e.g. `eu-frankfurt-1`), not for discovery.

---

## 5. The traps, with cause → fix → verification

These are the specifics behind your three symptoms (`localhost:3001`, CORS errors, 4 GB upload failures). **None of them are mysterious; each one is a concrete config or build step.**

### T1 — `localhost:8000` / `localhost:3001` are frozen into the bundle at build time
**Cause [repo].** There are two different resolvers and they disagree:

* `src/server/backend-api.ts` → `getBackendApiBaseUrl()` = `BACKEND_INTERNAL_URL` **||** `NEXT_PUBLIC_API_URL` **||** `"http://localhost:8000"`.
* Eight API routes read `NEXT_PUBLIC_API_URL` **first**: `api/feedback/route.ts:17`, `api/fonts/route.ts:16`, `api/fonts/[fontName]/route.ts:21,62`, `api/fonts/upload/route.ts:17`, `api/tasks/billing-summary/route.ts:17`, `api/tasks/create/route.ts:17`, `api/upload/[action]/route.ts:16`.

`NEXT_PUBLIC_*` values are **inlined at build time**, including in server code. So whichever value `NEXT_PUBLIC_API_URL` had when `pnpm build` ran is what those routes call forever, regardless of what you set at runtime — `http://localhost:8000` from docker-compose, or `:3001` if it was pointed at the published frontend port. That is exactly the "it still talks to localhost:3001" symptom.
**Fix.** Two rules:
1. Never build with a localhost `NEXT_PUBLIC_API_URL`. In cloud mode set it at build time to the real browser-facing URL (or to empty/same-origin if all calls go through Next).
2. Change those eight routes to use `getBackendApiBaseUrl()` (runtime `BACKEND_INTERNAL_URL` first) and make the resolver refuse to fall back to `localhost` when `NODE_ENV=production`. Then `BACKEND_INTERNAL_URL` can be changed without rebuilding.
**Verification.** After `pnpm build`, `grep -r "localhost:8000\|localhost:3001" .next` must return nothing; then load the UI with DevTools → Network and assert every request is same-origin or the tailnet host, and restart the frontend with only `BACKEND_INTERNAL_URL` changed to confirm no rebuild is needed.

### T2 — CORS allowlist rejects the real frontend origin
**Cause.** `cors_origins` defaults to `http://localhost:3107`, `http://sp.localhost…`, `http://clipwright.localhost…` **[repo, config.py:254]**.
**Fix.** `CORS_ORIGINS` must contain **every** origin the browser uses: `http://localhost:3107` in production mode, plus the tailnet origin if the UI is served over Tailscale. No wildcard, no trailing slash, exact scheme+host+port.
**Verification.** `curl -i -X OPTIONS $BACKEND/api/tasks -H "Origin: http://localhost:3107" -H "Access-Control-Request-Method: GET"` → `200` + `access-control-allow-origin: http://localhost:3107`.

### T3 — bucket CORS is never configured (you cannot do it with boto3 on OCI)
**Cause.** OCI's S3 API does not expose `PutBucketCors` **[docs]**. Without a CORS rule the browser's preflight to the bucket fails, and opt-in header reads (`ETag`) fail. Export/downloads that use `fetch()` instead of a plain navigation also fail.
**Fix.** Configure CORS with the OCI CLI (`oci os bucket update --cors-rules`), allowing:
* `AllowedMethods`: `PUT, GET, HEAD`
* `AllowedOrigins`: `http://localhost:3107` (+ the tailnet origin if used)
* `AllowedHeaders`: `content-length, content-type, x-amz-*`
* `ExposeHeaders`: `ETag`, `x-amz-request-id`
**Verification.** From the browser DevTools console on the app origin:
`fetch(presignedPartUrl, {method:"PUT", body:new Blob(["x"])})` → response readable, no CORS error (a 403 from the signature test in §4.3 is a *different*, still-blocking failure — see T8).

### T4 — the 4 GB failure is the streaming fallback, not S3
**Cause.** If `MEDIA_STORAGE != "s3"` (or any `MEDIA_S3_*` value is missing), `/upload/authorize` answers `{"storage": "local"}` and the browser streams the entire file to the backend. That path requires `Content-Length`, runs for up to an hour inside Next (`maxDuration = 3600`) and is capped at `NEXT_PUBLIC_MAX_VIDEO_UPLOAD_BYTES` = 4 GB. Through a laptop-hosted Next server and a tailnet backend, multi-GB bodies die on timeouts, memory, or proxy limits — "failing around 4 GB".
**Fix.** Require `MEDIA_STORAGE=s3` in every cloud-mode start script, fail fast at boot if it isn't, and keep `NEXT_PUBLIC_MAX_VIDEO_UPLOAD_BYTES` ≤ the bucket quota (recommend **2 GB**, see §6).
**Verification.** `POST /upload/authorize` (authenticated) must return `uploadId` + `parts`, never `{"storage":"local"}` — assert it in a test and at boot. Then upload a >2 GB file through the UI and watch each `UploadPart` in DevTools, not a single multi-GB body to the backend.

### T5 — `next dev` in production
**Cause.** docker-compose runs the frontend with dev bind-mounts; a dev server holds every file in memory, recompiles on demand, and can't serve multi-GB streams reliably.
**Fix.** Build artefacts on the laptop: `pnpm build` then `PORT=3107 NODE_ENV=production node .next/standalone/server.js` (with `output: "standalone"` already set, `next start` is not the right entry point).
**Verification.** `curl -sI http://localhost:3107` → `x-nextjs-cache`/`ETag` present, no `__NEXT_DEV__`, `Server: Next.js` with a production header set; upload the same file that failed at 4 GB.

### T6 — the stack is x86-only by default
**Cause.** `docker-compose.yml` pins `platform: "${BACKEND_PLATFORM:-linux/amd64}"`, and the backend image builds `uv sync --extra cpu` **[repo]**. OCI A1 is **arm64**.
**Fix.** `BACKEND_PLATFORM=linux/arm64`, `BACKEND_CPU_ONLY=true`, rebuild the image on the VM (not on the laptop — emulated builds are unbearably slow and often produce unusable wheels).
**Prior evidence:** the whole `uv.lock` has aarch64/universal wheels for every requirement (0 packages without one), and `torch 2.11.0+cpu` ships `manylinux_2_28_aarch64` for cp311–cp314. Caveats: `openai-whisper 20250625` and `srt 3.5.3` are sdist-only but pure Python (no C extensions, so they install anywhere), and `openai-whisper` declares `triton` on Linux, which has **x86_64-only** wheels → install through `uv sync --frozen --extra cpu` and never `pip install openai-whisper` bare.
**Additional check during the first build:** several media modules `import cv2` at module import **[repo]**, and the Dockerfile does not install `libgl1`/`libglib2.0-0`. If `cv2` import fails in the arm64 image, add those two apt packages.
**Verification.** On the VM: `uname -m` → `aarch64`; then `docker compose exec backend python -c "import platform, torch; print(platform.machine(), torch.__version__, torch.get_num_threads(), torch.cuda.is_available())"` → `aarch64`, a `+cpu` build, 2 threads, `False`; then `python -c "import whisper; print(whisper.available_models())"` and `python -c "import cv2; print(cv2.__version__)"` separately, so a missing `libgl1` is unambiguous.

### T7 — boto3 checksum headers
See §4.3(3). Fix in code (§14), not config.

### T8 — signed `Content-Length` on part uploads
See §4.3(2). **The one thing that can invalidate the whole direct-upload design.** Test it first (§15, probe script) and only then migrate.

### T9 — `ListObjectsV2` may be unsupported
See §4.2. Test with the probe; if it fails, switch the six call sites to the v1 paginator (mechanical change, same pagination API).

### T10 — bucket policy/lifecycle scripts are AWS-shaped
`scripts/setup-media-storage.py` cannot run against OCI (no bucket policies, no SSE-KMS config, no PutBucketCors). A replacement OCI script is part of the work (§14).

### T11 — the fallback bucket duplicates everything
See §4.4. Leave `MEDIA_FALLBACK_*` empty.

### T12 — Neon + always-on crons = suspension
**Cause.** `worker` cron schedule **[repo]**: `check_media_storage` every 20 s, `replicate_pending_media` every 40 s, `cleanup_media` every 5 min, plus sweeps. Each touches Postgres. On Neon, compute never reaches the 5-minute idle threshold → 730 h × 0.25 CU ≈ **182.5 CU-hours/month** vs a **100 CU-hour** allowance → suspend until next month. Frontend polling (5 s task progress, 3 s export panel, 0.9 s AI prompt) makes it worse while the app is open.
**Fix.** Host Postgres on the VM (no metering). If you insist on Neon: gate the sub-hourly crons behind "work is pending" flags stored in Redis, and accept that a bursty month can still suspend the project.

### T13 — 2 OCPU, not 4/24
See §0/§2. Anything above 2 OCPU/12 GB is billable once the trial credit is gone.

### T14 — worker concurrency is hard-coded for a big machine
`max_jobs = 4` (worker) and `max_jobs = 16` (ai-worker) are literals **[repo]**. On 2 vCPU that means four concurrent ffmpeg/Whisper jobs thrashing and OOM-killing 12 GB. Fix: make them env-driven (`WORKER_MAX_JOBS`, `AI_WORKER_MAX_JOBS`), default 1 and 2 in cloud mode.

### T15 — timeouts tuned for a desktop CPU
`AI_EDIT_TIMEOUT_SECONDS = 120` **[repo]** is generous on a Ryzen 5 4600U and tight for `llama3.2:3b` on 2 Ampere cores. Raise to 600 in cloud mode (`AI_ANALYSIS_TIMEOUT_SECONDS` from the previous task stays as-is; the derived ARQ `job_timeout` is ≥ 10,800 s and needs no change on stronger hardware, but keep it ≥ your largest expected Whisper run on ARM).

### T16 — shared-filesystem assumptions
`flock`-based media cache leases and the replication outbox require the API and workers on the same host, same volume. The single-VM design satisfies this. Do not split the worker onto a second VM without revisiting `media_replication.py`.

### T17 — cookies and origins
`nextCookies()` + host-only cookies **[repo]**: the UI's origin must be stable. Serving the UI at `http://localhost:3107` in production mode is fine; do not alternate between `localhost` and `127.0.0.1` (different cookie jars, "randomly logged out"). If you also serve it over Tailscale, treat that as a second origin and set `BETTER_AUTH_TRUSTED_ORIGINS` accordingly.

### T18 — Lambda is not a drop-in
Package <10 MB (zip) / 250 MB (unzipped), no `flock`/shared disk, 15-minute execution ceiling, no long-lived dev-server semantics. Rebuilding the API as a Lambda requires splitting torch/ffmpeg into an always-on container service — at which point you already have a VM. **Recommendation: skip Lambda entirely.**

### T19 — 50,000 object-storage requests/month
A 2 GB upload at 64 MB parts = 32 `UploadPart` + 1 initiate + 1 complete + 3 presigns (presigning is local, free) ≈ **37 requests**. 20 such uploads ≈ 740. Head/Get per job and per playback ≈ a few dozen each. You will not hit 50k by normal use, but log the counter monthly; a runaway polling loop on `head_object` could.

### T20 — 20 GB vs 4 GB uploads
Six maximum-size files exceed the whole free tier. See §6.

---

## 6. Storage budget (20 GB)

| Item | Size |
|---|---|
| Source video, 30 min 1080p, H.264 (~8 Mbps) | ≈ 1.8 GB |
| Capped master (1080p/60 re-encode) if produced | ≈ 0.6–1.2 GB |
| Clip render (60–90 s, vertical 1080p) | ≈ 30–60 MB |
| Transcripts/editor JSON/thumbs | negligible (KB–MB) |

**Policy I recommend (needs your approval — D2, §18):**
1. App-level quota `MEDIA_STORAGE_QUOTA_BYTES=16106127360` (15 GiB), leaving headroom for transient multipart parts.
2. Per-file cap `NEXT_PUBLIC_MAX_VIDEO_UPLOAD_BYTES=2147483648` (2 GB) ≈ 45 min of 1080p or 90 min of 720p.
3. Quota is enforced in `reserve_upload` (sum of live objects) **before** presigning, with a clear "storage full — delete a project or move it to the archive" error. Never a silent failure.
4. Show usage in Settings ("Storage 11.4 / 15 GB").
5. No automatic deletion of originals — the existing sweeps only remove orphans/pending payloads, and that must stay true.
6. Optional archive: move originals > N days old to the VM's 150 GB block volume with a stamp file, and restore on demand. This costs nothing and keeps originals; it is extra code and I'd do it in a second pass.

---

## 7. Where each orchestration layer is defined

| Concern | Definition | Cloud value |
|---|---|---|
| Backend URL used server-side | `BACKEND_INTERNAL_URL` (`frontend/src/server/backend-api.ts`) | `http://<tailscale-host>:8000` |
| Backend URL used by the browser | `NEXT_PUBLIC_API_URL` | same-origin proxy (empty) or tailnet HTTPS |
| Backend CORS | `CORS_ORIGINS` | `http://localhost:3107` |
| Auth origin | `BETTER_AUTH_URL`, `NEXT_PUBLIC_APP_URL`, `BETTER_AUTH_TRUSTED_ORIGINS` | `http://localhost:3107` |
| Database | `DATABASE_URL` (frontend, Prisma) / backend env | `postgresql://…@127.0.0.1:5432/supoclip` through an SSH tunnel to the VM |
| Redis | `REDIS_URL` | `redis://redis:6379/0` (compose network only) |
| Media | `MEDIA_STORAGE`, `MEDIA_S3_*` | `s3` + OCI |
| Local AI | `OLLAMA_BASE_URL`, `WHISPER_MODEL_SIZE`, `OFFLINE_MODE` | `http://ollama:11434`, `base`/`small`, `true` |

---

## 8. Database decision (D1)

**Recommendation: Postgres on the VM.**
* Zero metering, zero suspension, no 1 GB ceiling, no 5 GB transfer ceiling.
* Backend/workers connect over the compose network (sub-millisecond); the laptop's Prisma connects through an SSH tunnel to the VM's loopback (no published port).
* Fits comfortably in the 200 GB block volume (a metadata-only DB is tens to low hundreds of MB; even a few GB of transcripts is fine).
* Both migration runners run once, from the VM.

**Neon is acceptable only if** you (a) accept §5-T12's cron gating work, (b) measure the DB under 800 MB, and (c) accept that a heavy month suspends the project until the 1st. The 5 GB transfer ceiling is also easy to trip if any endpoint returns full transcripts in list responses — worth auditing before choosing Neon.

**Measurement commands (Phase 0, run on the laptop):**

```powershell
# database size + per-table sizes + row counts for the tables that grow
docker compose exec -T postgres psql -U supoclip -d supoclip -c `
  "SELECT pg_size_pretty(pg_database_size('supoclip')) AS total;"
docker compose exec -T postgres psql -U supoclip -d supoclip -c `
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) sz
     FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 20;"
docker compose exec -T postgres psql -U supoclip -d supoclip -c `
  "SELECT (SELECT count(*) FROM tasks) tasks,
          (SELECT count(*) FROM sources) sources,
          (SELECT count(*) FROM generated_clips) clips;"
# media on disk
Get-ChildItem -Recurse .\data\uploads, .\data\clips |
  Measure-Object -Property Length -Sum |
  Select-Object Count, @{n='GB';e={[math]::Round($_.Sum/1GB,2)}}
```

---

## 9. Media migration procedure (copy, verify, never delete)

Existing runbook: `backend/scripts/migrate_media_to_s3.py` (report-only by default; `--apply`; `--release-cleanup TOKEN`). **[repo]**

1. **Report** — on the **laptop**, from `backend/` with the backend venv (`PYTHONPATH=backend python -m scripts.migrate_media_to_s3 --report`; that directory is not baked into the container image) … dry run lists skips (active tasks, multi-owner, oversize, missing files) and the total bytes to copy. Confirm the total is ≤ 15 GB **before** provisioning anything.
2. **Backup** — `pg_dump` the database and `robocopy` `./data` to an external drive. Originals remain the source of truth until step 5.
3. **Apply** — `--apply` copies objects and rewrites references through `ClipRepository.update_clip`, which **queues a deletion of the previous path** **[repo]**. That is why step 5 is separate: nothing is deleted until `--release-cleanup`.
4. **Verify** — for a sample of clips and sources: `head_object` size matches, a presigned GET returns the first kilobyte, the editor document still opens, and one clip re-renders. Batch-verify with checksums for everything if the total is small.
5. **Release** — only after 2 weeks of clean operation (or when disk is tight): `--release-cleanup <token>`.
6. **Rollback at any point** — point `MEDIA_STORAGE=local` again on the laptop and restart; the local files are still there until step 5.

---

## 10. Cutover and restart

1. Stop the laptop's `backend`, `worker`, `ai-worker` (keep the frontend).
2. Start the cloud stack: `docker compose -f docker-compose.cloud.yml up -d backend worker ai-worker redis postgres ollama`.
3. Point the laptop at it: `BACKEND_INTERNAL_URL=http://<tailnet-host>:8000`, `DATABASE_URL=…@127.0.0.1:5432/supoclip` (SSH tunnel open), then **rebuild** (`pnpm build`) — `NEXT_PUBLIC_*` values are inlined at build time — and start with `PORT=3107 node .next/standalone/server.js`.
4. Smoke test: status page shows Ollama reachable, Whisper model present, storage = S3 with N GB used.
5. Import → transcribe → select → edit → export → save → **restart everything** → reopen the project, clip intact.

**Rollback:** reverse step 3 (back to `localhost:8000` + local Postgres) and restart the laptop stack. The bucket keeps whatever was uploaded; nothing is destroyed.

---

## 11. Security review (no public surfaces)

| Asset | Exposure |
|---|---|
| Postgres | **no published port**; reachable only from the VM's loopback through an SSH tunnel (recommended) or a port bound to the `tailscale0` address. Password auth. OCI security list: no ingress on 5432. |
| Redis | compose network only, **no published port**. |
| Ollama | compose network only (`http://ollama:11434`), **no published port**, models in a volume. (On a Linux host Ollama binds 127.0.0.1 and is unreachable from containers unless forced; running it as a compose service avoids that whole class of bug and is the cleanest answer to "all three workers must prove they can reach it".) |
| Backend :8000 | keeps the repo's `127.0.0.1:8000:8000` binding **[repo]**; `tailscale serve --bg 8000` puts it on the tailnet with a valid `*.ts.net` TLS certificate — no public listener, no domain purchase, **no compose port change**. |
| Object storage | private bucket, no public ACLs, no bucket policy (OCI ignores AWS-style policies); access only via presigned URLs (900 s TTL, `MEDIA_URL_TTL_SECONDS`). Customer Secret Key scoped to one bucket by IAM policy. |
| Secrets | `.env` files on the VM with `600`; never in compose, never logged (the code already avoids logging credentials/signed URLs). Rotate the customer secret key by regenerating the pair. |
| SSE-C | optional; OCI supports it, but it makes every GET need the key header — skip unless you want it. |

---

## 12. Zero-cost guardrails

1. Never resize the A1 beyond **2 OCPU / 12 GB** (Always Free ceiling).
2. Stay in the **home region** for compute and storage. Cross-region access is allowed but billable in some paths; keep it simple.
3. Object storage **≤ 20 GB** and **≤ 50,000 requests/month**; the app-level quota is the enforcement.
4. **Stay on the Free tier.** Overuse on Free suspends/serves errors; the same overage on Pay As You Go becomes an invoice. (Oracle may pressure you toward PAYG to avoid idle-reclaim; that is a conscious trade, not a free upgrade.)
5. Do not enable a fallback/replica bucket or a second provider.
6. `docker system prune` monthly + cap container logs (`max-size: 10m`, `max-file: 2`) so the 200 GB block volume doesn't fill with logs.
7. The $300 trial credit expires after 30 days — assume it is already gone.
8. Check the billing dashboard on day 30 to prove $0 (the only real proof).

---

## 13. Performance expectations on 2 OCPU / 12 GB Ampere

Estimates only (**[assumption]** — to be measured after cutover, on your real sources):

| Task | Ryzen 5 4600U (6C/12T) | 2 OCPU A1 | Notes |
|---|---|---|---|
| Whisper `small` (244 M params), 30-min video | ~10–20 min | ~1.5–3.5 h | CPU-only fp32 torch |
| Whisper `base` (74 M), 30-min video | ~3–6 min | ~40–80 min | **Recommended first setting** |
| `llama3.2:3b` Q4 generation | ~10–15 tok/s | ~3–6 tok/s | clip selection for a 30-min transcript = many chunked calls → several minutes |
| ffmpeg 1080p H.264 render of a 60–90 s clip | ~20–40 s | ~1.5–3 min | |
| Concurrency | 4 jobs fine | **1 worker job, 2 ai jobs max** | otherwise swap/OOM |

Practical implication: transcription becomes a *background* task you start and walk away from; keep `WHISPER_MODEL_SIZE=base` on the cloud, `small` locally if you ever run local. If the cloud turns out too slow, the clean mitigation is a small paid instance of your choice — but that is a deliberate decision, not a default.

---

## 14. Code and config changes required (post-approval, file by file)

| # | File | Change | Why |
|---|---|---|---|
| 1 | `backend/src/media_storage.py` | primary boto3 client: pin `addressing_style="path"`, `request_checksum_calculation="when_required"`, `response_checksum_validation="when_required"`, `connect_timeout`/`read_timeout`; optional `MEDIA_S3_SIGN_PART_LENGTH=false` to omit the signed `ContentLength`; v1 paginator fallback for prefix listings | §4.3, T7–T9 |
| 2 | `backend/src/config.py` | new settings: `MEDIA_S3_ADDRESSING_STYLE`, `MEDIA_S3_CHECKSUMS`, `MEDIA_S3_SIGN_PART_LENGTH`, `MEDIA_STORAGE_QUOTA_BYTES`, `WORKER_MAX_JOBS`, `AI_WORKER_MAX_JOBS` (+ validation, defaults unchanged so nothing regresses locally) | T8, T14, §6 |
| 3 | `backend/src/workers/tasks.py`, `ai_worker.py` | `max_jobs` from config | T14 |
| 4 | `backend/src/services/upload_repository.py` (or `media_storage.begin_upload`) | enforce `MEDIA_STORAGE_QUOTA_BYTES` before presigning; typed error surfaced to the UI | §6 |
| 5 | `scripts/oci-object-storage-setup.sh` (**new**) | OCI CLI: bucket in root compartment, private, versioning off, `--cors-rules`, lifecycle to abort incomplete multipart uploads after 1 day, IAM policy scoped to `bucket clipwright-media`, customer secret key; writes `MEDIA_S3_*` to a `600` env file; never echoes secrets | T3, T10 |
| 6 | `backend/src/tools/verify_media_storage.py` (**new** — under `src/` because only `src/`, `fonts/` and `transitions/` are copied into the image, see `backend/Dockerfile`) | S3-compat probe: create multipart → presign part → PUT with exact `Content-Length` → `ListParts` → complete → `HeadObject` → presigned GET with `response-content-disposition` → `ListObjectsV2` → delete. Prints a named pass/fail line per step, exits non-zero on the first failure | T8, T9 — **run before anything else** |
| 7 | `docker-compose.cloud.yml` (**new**, additive) | `frontend`/`mcp`/`bgutil`/`vector` removed; `platform: linux/arm64` + build arg `BACKEND_CPU_ONLY=true`; **no published ports for redis/ollama/postgres**; `ollama` enabled; log rotation; memory limits | §3, §11 |
| 8 | `.env.cloud.example` (**new**) | complete cloud env template with comments, no secrets | §7 |
| 9 | `docs/cloud-deployment.md` (**new**, after approval) | the runbook: provision, verify, migrate, cut over, roll back | deliverable |
| 10 | `frontend/package.json` + the 8 `src/app/api/**/route.ts` files listed in T1 + `frontend/src/server/backend-api.ts` | `"start:prod": "PORT=3107 node .next/standalone/server.js"`; make every route use the runtime `BACKEND_INTERNAL_URL` resolver; no `localhost` fallback in production | T1, T5 |
| 11 | tests | `test_media_storage_oci.py` (addressing style, checksums, signed-part-length toggle, paginator fallback), `test_storage_quota.py`, `test_worker_concurrency.py`, `test_cloud_compose.py` (arm64 + no published redis/ollama + quota in env), plus an opt-in live probe test skipped unless `MEDIA_PROBE=1` | §15 |

No database schema change, no rename of any existing identifier, no change to the local/Docker dev path.

---

## 15. Verification plan (what "done" means)

Ordered so that the risky items fail early and cheaply:

1. **Compatibility probe** (`verify_media_storage.py`) — before any migration. Steps that can fail: signed part length, `ListObjectsV2`, `response-content-disposition` on presigned GET.
2. **Browser upload through the real UI** — 300 MB file, watch each `UploadPart` return 200 with a readable `ETag`. Then a 2 GB file.
3. **Playback** — `GET /api/clips/<id>` returns 307 and the browser pulls bytes from OCI (DevTools: request goes to the bucket host, not to the laptop).
4. **Offline-independence of local workflows** — unchanged from the previous milestone: with internet blocked (loopback + tailnet allowed), import/transcribe/edit/export still work *if* media stays on the VM volume; with media in the bucket, "offline" now means "no internet", so the honest test is: **bucket absent → app degrades with a clear error; only the online features are affected**. (This is a real trade-off of moving storage to the cloud — see §18-D2.)
5. **Restart persistence** — reboot the VM, restart the laptop server, reopen a project; also `docker compose down && up` and confirm Redis/PG volumes survive.
6. **Workers reach Ollama** — `docker compose exec worker curl -fsS http://ollama:11434/api/tags` and the same from `backend` and `ai-worker`, plus the in-app status endpoint.
7. **ARM image sanity** — `torch`, `whisper`, `cv2`, `ffmpeg -version` inside the container.
8. **Regression** — the full existing suites must stay at their current baseline (backend unit: 11 pre-existing failures, 1551 passed; frontend vitest: 635 passed; eslint clean). Any change beyond that is a real regression.
9. **Billing check** — after 72 h of running: OCI console → Cost Analysis → $0.00, and Object Storage usage < 20 GB.

---

## 16. Risks and unknowns

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OCI rejects presigned part URLs that sign `Content-Length` | medium | blocks direct upload | probe first; toggle `MEDIA_S3_SIGN_PART_LENGTH=false` → validate via `ListParts` |
| `ListObjectsV2` unsupported | medium | editor restore/sweeps break | paginator fallback to v1 |
| A1 capacity unavailable at signup ("out of capacity") | medium | can't provision | try another region; retry for a few days; AMD micro as a stopgap (too slow for Whisper, OK for API only) |
| 20 GB bucket too small for the library | high (if you keep many videos) | forced cleanup | quota + archive-to-VM-disk (§6) |
| ARM64 image build issues (cv2/libGL, deno download) | medium | build failure | first build on the VM, add missing apt packages |
| ARM CPU too slow for `small` Whisper | high | long waits | `base` default; measure; keep local option |
| Tailscale dependency for laptop↔cloud | low | no DB/API access | documented `ssh -L` tunnel as a fallback (`ssh -N -L 5432:localhost:5432 -L 8000:localhost:8000`) |
| Free tier ToS: one account, no multiple free accounts | — | account termination | single tenancy, no workarounds |

---

## 17. What I did **not** do

* No Oracle/AWS/Neon/Vercel account, CLI, or MCP call.
* No bucket, instance, policy, key, or DNS record created.
* No `.env`, `docker-compose.yml`, database, or container touched.
* No media uploaded or moved; `migrate_media_to_s3.py` was read, not run.
* No file in the repo modified except this document.

---

## 18. Approval checklist

Please confirm or change:

* **D1 — Database:** Postgres on the VM (**recommended**) or Neon with the cron-gating work?
* **D2 — Media budget:** 15 GiB app quota + 2 GB per-file cap in the 20 GB bucket (**recommended**), or hybrid with older originals archived to the VM's 150 GB block volume? Note that with media in the cloud, "works with the internet unplugged" no longer covers *existing* projects — only the app shell and anything already cached.
* **D3 — Whisper size on the cloud:** `base` (**recommended**) or `small`?
* **D4 — UI exposure:** laptop-only `http://localhost:3107` (**recommended**), or also reachable on the tailnet from other devices?
* **D5 — Region:** which OCI region (bucket home region = compute region)? Frankfurt/Amsterdam are the usual low-latency picks from Lagos; capacity varies.
* **D6 — Scope of my next step:** probe script + OCI setup script + compose/env + docs (§14 items 1–9), or the smaller subset of just the probe and the plan-to-code for storage compatibility first?

Once you answer D1–D6 I will implement in this order: probe script → storage client changes → compose/env → quota → docs → regression suite, and report real command output at each step.

---

## Appendix A — OCI endpoint forms **[docs]**

```
# path-style (what boto3 uses with a custom endpoint, verified [test])
https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
# virtual-hosted style
https://<bucket>.vhcompat.objectstorage.<region>.oci.customer-oci.com
```

`MEDIA_S3_REGION` must be the OCI region identifier (`eu-frankfurt-1`, `eu-amsterdam-1`, `us-ashburn-1`, …) because it is the SigV4 signing region. The namespace is per tenancy and immutable. Bucket names must be unique within the namespace.

## Appendix B — laptop commands (PowerShell, Windows 11)

```powershell
# 1. Tailscale on both ends
winget install --id Tailscale.Tailscale
tailscale up
tailscale status                       # note the VM's 100.x.y.z

# 2. Build and run the UI in production mode
cd C:\path\to\Supoclip-Clipwright\frontend
pnpm install --frozen-lockfile
pnpm prisma generate
pnpm build
$env:BACKEND_INTERNAL_URL="http://100.x.y.z:8000"
$env:NEXT_PUBLIC_API_URL="http://100.x.y.z:8000"
$env:BETTER_AUTH_URL="http://localhost:3107"
$env:NEXT_PUBLIC_APP_URL="http://localhost:3107"
# keep the tunnel open in a second terminal:
#   ssh -N -L 5432:localhost:5432 ubuntu@100.x.y.z
$env:DATABASE_URL="postgresql://supoclip:<pw>@127.0.0.1:5432/supoclip"
$env:PORT="3107"
$env:NODE_ENV="production"
node .next\standalone\server.js     # serves http://localhost:3107

# 3. Prove the backend is reachable and CORS is right
curl.exe -i -X OPTIONS http://100.x.y.z:8000/tasks/ `
  -H "Origin: http://localhost:3107" -H "Access-Control-Request-Method: GET"
```

## Appendix C — VM commands (Ubuntu 24.04 arm64)

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 tailscale
sudo tailscale up

# build the arm64 images on the VM itself
export BACKEND_PLATFORM=linux/arm64 BACKEND_CPU_ONLY=true
docker compose -f docker-compose.cloud.yml build

# start
docker compose -f docker-compose.cloud.yml up -d

# prove every worker can reach Ollama  (must be run for all three)
for svc in backend worker ai-worker; do
  docker compose -f docker-compose.cloud.yml exec -T $svc \
    curl -fsS http://ollama:11434/api/tags | head -c 200; echo " <- $svc";
done

# prove Whisper loaded, on CPU, once
docker compose -f docker-compose.cloud.yml exec -T ai-worker \
  python -c "import whisper,torch; m=whisper.load_model('base', download_root='/models/whisper'); print(torch.__version__, m.dims)"

# expose the API on the tailnet only, with a real certificate
sudo tailscale serve --bg 8000          # https://<host>.<tailnet>.ts.net -> 127.0.0.1:8000

# prove the storage path before migrating anything
docker compose -f docker-compose.cloud.yml exec -T backend \
  python -m src.tools.verify_media_storage
```

## Appendix D — references

* Oracle Cloud Free Tier allowances: https://www.oracle.com/cloud/free/ (fetched 2026-11-01)
* Object Storage S3 Compatibility API: https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm
* S3 compatibility support matrix: https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm
* Neon pricing/limits: https://neon.com/pricing (fetched 2026-11-01)
* Existing media runbook: `docs/media-storage.md`
* Existing media migration script: `backend/scripts/migrate_media_to_s3.py`
