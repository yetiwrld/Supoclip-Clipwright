# Rebranding notes: SupoClip → ClipWright Studio

ClipWright Studio is a fork of [SupoClip](https://github.com/FujiwaraChoki/supoclip)
(AGPL-3.0) that was rebranded and re-pointed at local models. This page records
what was renamed, what was deliberately kept, and why — so an upgrade or a
`git grep` does not turn into archaeology.

## What was renamed

| Surface | Before | After |
|---|---|---|
| Product name (all UI, docs, errors, metadata) | SupoClip | ClipWright Studio |
| Tab title / OpenGraph / manifest name | SupoClip | ClipWright Studio |
| Logo and icons | SupoClip mark | the supplied ClipWright icon (see below) |
| Email addresses | `support@`/`privacy@supoclip.com` | `support@`/`privacy@clipwright.example` |
| Marketing domain | `https://www.supoclip.com` | `http://localhost:3107` (override with `NEXT_PUBLIC_APP_URL`) |
| Copyright holder line | SupoClip Team | ClipWright Studio contributors (upstream notice kept in `LICENSE` / `NOTICE`) |
| Docker container names | `supoclip-*` | `clipwright-*` |
| Default data root | scattered | `./data/{uploads,clips,models}`, override `CLIPWRIGHT_DATA_DIR` |
| Default AI | hosted LLM + AssemblyAI | Ollama `llama3.2:3b` + local Whisper `small` |
| Default theme | light | dark |

Searching for `SupoClip` now returns only `LICENSE`/`NOTICE` attribution, the
upstream GitHub URL, blog slugs, and the marketing copy that explicitly credits
the upstream project.

## What was deliberately **not** renamed

Renaming these would break running deployments, existing databases, queued jobs
or third-party clients, so they are frozen:

| Identifier | Where | Why kept |
|---|---|---|
| `x-supoclip-user-id`, `x-supoclip-ts`, `x-supoclip-signature` | `backend/src/main.py`, `backend/src/auth_headers.py` | Signed header contract between frontend proxy and backend; both sides and any deployed pair must match |
| ARQ queue names `supoclip_tasks`, `supoclip_fast` | `backend/src/workers/job_queue.py` | In-flight Redis jobs would be orphaned on rename |
| PostgreSQL user/database `supoclip`, `supoclip_password` | `docker-compose.yml`, `.github/workflows/tests.yml` | Existing volumes already contain that role; renaming means data migration |
| Python distribution `supoclip-backend` | `backend/pyproject.toml` | Package identity used by lockfiles and image builds |
| `supoclip_mcp` package and `supoclip_*` MCP tools | `mcp/` | Tool names are a public interface for MCP clients; documented as internal |
| Blog slugs (`/blog/supoclip-vs-opusclip`, …) and `supoclip-editor.webp` | `frontend/messages/*/marketing.json`, `frontend/public/blog/` | Stable URLs and already-indexed assets |
| `SUPOCLIP_SERVICE` env marker, `supoclip_` metric prefixes | backend observability | Dashboards and alerts key off them |
| Upstream URLs and licence notices | `LICENSE`, `NOTICE`, README, docs | AGPL-3.0 attribution must survive |

Anything a user reads says ClipWright Studio; anything a machine stores stays as
it was.

## Logo assets

The logo was supplied by the project owner (`ClipWright Icon.png`,
1254 × 1254 RGBA, transparent background). It is stored **once** as a master and
everything else is derived from it:

```
frontend/public/brand/clipwright-logo.png    master, unmodified (1254×1254, RGBA)
frontend/public/brand/clipwright-icon-512.png  derived (manifest any + maskable)
frontend/public/brand/clipwright-icon-192.png  derived (manifest any)
frontend/public/brand/clipwright-icon-180.png  derived (apple-touch-icon)
frontend/public/brand/clipwright-icon-64.png   derived
frontend/public/brand/clipwright-icon-32.png   derived (favicon 32)
frontend/public/brand/clipwright-icon-16.png   derived (favicon 16)
frontend/public/brand/clipwright-icon-1024.png derived (store/social previews)
frontend/public/brand/favicon.ico              derived (16/32/48)
frontend/src/app/favicon.ico                   copy for the Next.js convention
```

Rules enforced by the code:

- `frontend/src/lib/brand.ts` is the single source of sizes/paths; nothing else
  hardcodes an image path.
- `frontend/src/components/brand-mark.tsx` is the only component that renders the
  logo; it never applies `dark:invert`, CSS filters or recolouring.
- Images render with `unoptimized` and explicit `width`/`height` (or `fill` in a
  fixed-ratio box) so the aspect ratio cannot be stretched and transparency is
  preserved.
- The old SupoClip logo files were deleted; no generic placeholder is used
  anywhere.

To replace the master logo later, overwrite `clipwright-logo.png` and re-derive
the sizes (keep the file names so no component changes are needed):

```bash
python3 - <<'EOF'
from PIL import Image
src = Image.open("frontend/public/brand/clipwright-logo.png").convert("RGBA")
for size in (1024, 512, 192, 180, 64, 32, 16):
    src.resize((size, size), Image.LANCZOS).save(
        f"frontend/public/brand/clipwright-icon-{size}.png", optimize=True)
src.resize((256, 256), Image.LANCZOS).save(
    "frontend/public/brand/favicon.ico",
    sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
EOF
cp frontend/public/brand/favicon.ico frontend/src/app/favicon.ico
```

## Theme

- Tokens live in `frontend/src/app/globals.css` (CSS custom properties) and are
  exposed to Tailwind as semantic classes (`bg-background`, `text-muted-foreground`,
  `border-border`, `bg-card`, `text-primary`, …). Components do not carry
  hardcoded colours, and no `stone-*`/`bg-white` palette classes remain outside
  deliberate white-on-media cases.
- Dark is the default: `<html class="dark">` is static, `NextThemesProvider` uses
  `defaultTheme="dark"` with `enableSystem={false}`, so a machine set to light
  mode still opens dark.
- The choice is persisted under the `clipwright-theme` localStorage key and the
  toggle only offers light/dark (no "system" surprises).
- Landing, auth, app shell, editor, timeline, dialogs, toasts, tables and
  tooltips all read the same tokens, so a rebranded accent changes one file.

## Behaviour changes worth knowing when upgrading

1. **No API key required.** `.env` no longer needs `ASSEMBLY_AI_API_KEY` or a
   hosted LLM key. `OFFLINE_MODE=true` refuses hosted AI even if a key exists.
2. **Downloads are explicit.** Starting the app never fetches weights; run
   `scripts/setup_local_ai.py --install-whisper` and `ollama pull llama3.2:3b`.
3. **Workers preflight.** Jobs fail immediately with an actionable message when
   the Whisper checkpoint or Ollama model is missing.
4. **Data root moved.** Media now lives under `./data` (bind mounts) instead of
   anonymous volumes, so `docker compose down` no longer risks losing sources.
5. **Names in Compose changed.** Any script or `docker exec` using
   `supoclip-postgres` must switch to `clipwright-postgres`.
