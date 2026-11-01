# Phase 2 design — Oracle ↔ Kaggle job exchange (design only, not implemented)

Status: **design**. Nothing in this document has been built, and nothing in Phase 1
contains an Oracle client. It exists so the Phase 1 prototype can be judged against
a concrete integration story.

Constraints carried over from the approved architecture and the safety rules:

* Oracle Always Free A1 (2 OCPU / 12 GB, arm64) VM owns Postgres, Redis, ARQ
  workers, media storage and Ollama. It is the source of truth.
* Kaggle is **burst compute only**. It is never an always-on worker, never a replica,
  never a database host.
* Postgres, Redis and Ollama are **never** exposed to Kaggle. Only one narrowly
  scoped HTTP surface is ever reachable.
* No paid services, no paid proxies, no bypassing access controls.
* Local/VM data is not touched until a cloud install has passed a full workflow test.

---

## 1. Roles

| Component | Owns |
|---|---|
| Oracle backend (FastAPI) | job records, state machine, leases, artefact ingest, audit |
| Oracle ARQ workers | local jobs, retries, cleanup, replication |
| Kaggle kernel | one leased job: download → transcribe → select → render → upload |
| ClipWright Studio (laptop) | UI only, talks to the Oracle backend over Tailscale |

A Kaggle job is an *ARQ task that runs elsewhere*. The database row is the same shape
as a local task row plus `executor = "kaggle"`, so the editor and project UI need no
new concepts.

---

## 2. Job lifecycle

```
queued ──lease──▶ leased ──upload_input──▶ input_ready ──kernel_start──▶ running
   ▲                                             │                          │
   │                                             └────lease expired────◀───┤
   └──────────────── retry (max 2) ◀────────────────────────────────────  │
                                                                           ▼
                                        validating ◀──upload_outputs── rendering
                                             │
                                    ┌────────┴────────┐
                                    ▼                 ▼
                                 complete           failed
```

* **Lease**: the kernel POSTs `lease_id` + `job_id`; the backend returns a
  server-side expiry (default 30 min, renewed by heartbeat every 60 s). A kernel that
  dies loses its lease and the job returns to `queued`; the partial upload is
  discarded by `attempt` number, never merged.
* **Idempotency**: every endpoint is keyed on `(job_id, attempt)`. Re-running a
  kernel with the same lease is a no-op after `input_ready`.
* **12-hour cap**: the kernel self-aborts at 11 h 15 m (`processing.deadline_seconds`,
  default 40 500) and uploads whatever passed validation, marked `partial`. A job
  that would exceed the cap is rejected at queue time by a cost estimate
  (source duration × measured RTF), not discovered at hour 11.

---

## 3. What crosses the wire

Input (Oracle → Kaggle), one file per job:

```
GET  /api/kaggle/jobs/{job_id}/input/{attempt}     → 302 to a single-use, 2 h,
                                                      size-pinned URL
GET  /api/kaggle/jobs/{job_id}/job.json            →  the manifest this phase already
                                                      validates (same schema/v1)
POST /api/kaggle/jobs/{job_id}/heartbeat
```

Output (Kaggle → Oracle), per artefact:

```
POST /api/kaggle/jobs/{job_id}/outputs/{name}/begin   → presigned PUT, max size
PUT  <presigned>
POST /api/kaggle/jobs/{job_id}/outputs/{name}/commit  → sha256 + size + ffprobe JSON
POST /api/kaggle/jobs/{job_id}/result                → the result manifest
```

Rules:

* `commit` re-verifies the SHA-256 the kernel declares in `result.json` (the same
  check `scripts/output.ps1` already performs locally). A mismatch fails the job and
  keeps the bytes quarantined for inspection.
* Names are validated against a fixed set (`clips/*.mp4`, `captions/*.ass`,
  `transcript.json`, `ai-selection.json`, `benchmark.json`, `REPORT.md`). Path
  traversal, symlinks and unexpected content types are rejected.
* Output size is capped by the *project's* configured ceiling, not by trust in the
  manifest.
* Uploads land in the existing `MEDIA_STORAGE=local` layout under a per-attempt
  staging directory, then move into place with a DB transaction that flips the job
  to `complete`. A crash leaves an orphan staging dir that the existing cleanup cron
  already handles.

---

## 4. Authentication

Problem: a Kaggle notebook cannot hold a long-lived secret (source is reviewed, logs
are shared, outputs are public unless the kernel is private).

Design:

1. The Oracle backend issues a **job token**: HMAC-signed, scoped to
   `(job_id, attempt)`, ≤ 12 h, and *only* valid for the five endpoints above. It is
   useless for any other API route.
2. Delivery: the token is written into **Kaggle notebook secrets** by an operator
   action (`kaggle kernels push` cannot set secrets), or pasted into the job.json of
   a private input dataset. Never in `kernel-metadata.json`, never in source, never in
   output.
3. The kernel reads it from `os.environ` and holds it in memory only. Logs redact it
   (`_redact()` in the kernel already strips values of credential-shaped keys).
4. Transport: Tailscale. The Oracle VM joins the tailnet; the kernel joins as an
   ephemeral node (`tailscale up --authkey …`, authkey also a secret, single-use,
   1 h expiry). No public listener, no open port, no `0.0.0.0`.
5. Alternative if Tailscale-in-notebook proves fragile: an outbound-only tunnel from
   the VM (Cloudflare Tunnel free tier, `cloudflared`) exposed with an access policy.
   Decided at implementation time by testing both, not by preference.

What is explicitly rejected: publishing the backend, exposing Postgres/Redis,
`ngrok`-style public tunnels without an access policy, storing a permanent API token
in the kernel source, and the user's own laptop acting as the transfer relay.

---

## 5. Where the video actually comes from

Kaggle cannot pull from the VM's Tailscale address without the tunnel above, and
private datasets cap at 20 GB compressed. Three options, in the order they should be
tested:

1. **Operator upload (default).** The clip is attached as a private dataset, exactly
   as Phase 1 does. No network path from Oracle to Kaggle is needed for input at all —
   the smallest moving part wins.
2. **Tunnel pull.** Kernel downloads the input over the job token via the Tailscale
   path, capped at `source.max_bytes` (default 2 GiB) and 3 h wall clock, which
   `downloader.py` already enforces for HTTP sources.
3. **Deferred.** Object storage as an intermediate (rejected in the v2 spec for now;
   the deferred section of `docs/cloud-deployment-spec.md` covers when that changes).

Long jobs stay local on the Oracle VM. Kaggle is used when the GPU genuinely helps:
batch backfill, heavy re-selection, or a project too long for 2 OCPUs to chew
through in one night — measured, not assumed.

---

## 6. Failure handling

| Failure | Detection | Outcome |
|---|---|---|
| Kernel killed (quota, 12 h) | no heartbeat for 10 min | lease expires, job → `queued`, `attempt + 1` |
| GPU not granted | `require_gpu` gate, exit 2 | job fails fast with "no GPU assigned"; never a CPU result labelled GPU |
| CUDA/driver error mid-run | exception in render/transcribe | exit 7, artefacts validated, job `failed` with the gate name |
| Output checksum mismatch | `commit` re-hash | job `failed`, bytes quarantined, previous good attempt untouched |
| Partial output | Gate G failures | job `partial`; UI offers "accept the clips that passed" or retry |
| Ollama-equivalent model absent on Kaggle | `build_backend` reports both backends | exit 7 before any render (never a cloud fallback) |
| Token expired | 401 with `reauth_required` | kernel exits 3; the backend re-issues on the next lease |

Every one of these maps onto an existing state the ClipWright UI already renders for
local tasks, so no new frontend vocabulary is required.

---

## 7. What Phase 2 must prove before it ships

Same gate discipline as the cloud spec:

1. Real short video, end-to-end through the tunnel, outputs re-verified by SHA-256.
2. A killed kernel resumes correctly (lease expiry path exercised on purpose).
3. No Postgres/Redis/Ollama port is reachable from the notebook
   (`nmap`/`nc` from the running kernel, output pasted in the report).
4. Token scoping test: the job token gets 403 on `/api/projects`, `/api/media` and
   `/api/settings`.
5. Cost check: measured wall time × core/GPU minutes against the free quota, with the
   quota-exhaustion behaviour written down.
6. Data retention: what the kernel *leaves* in `/kaggle/working` and in its private
   dataset, and a documented deletion step.

Until gates 1–6 pass, production stays local-only and this document stays a design.
