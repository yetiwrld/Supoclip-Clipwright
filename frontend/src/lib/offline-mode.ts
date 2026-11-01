

/**
 * Server-side Offline Mode resolution, shared by the layout (analytics scripts),
 * the PostHog server client and the error instrumentation.
 *
 * Two layers, same as the backend:
 *   1. the runtime switch written from Settings (`<config>/offline-mode.json`),
 *   2. the `OFFLINE_MODE` environment default (on when unset).
 *
 * The config directory is mounted read-only into the frontend container, so the
 * switch written by the API is visible here without a restart or an HTTP call.
 * Anything unreadable falls back to "offline": privacy fails closed.
 */

const FILE_NAME = "offline-mode.json";

function configDirectory(): string | null {
  const explicit = process.env.CLIPWRIGHT_CONFIG_DIR?.trim();
  if (explicit) return explicit;
  const dataDir = process.env.CLIPWRIGHT_DATA_DIR?.trim();
  // Forward slashes are valid separators on Windows too, and this keeps
  // `node:path` out of the module (see the note above).
  return dataDir ? `${dataDir.replace(/[\\/]+$/, "")}/config` : null;
}

/**
 * Read a file without a static `node:` import.
 *
 * `next build` compiles `instrumentation.ts` for the edge runtime as well, and
 * an `import fs from "node:fs"` anywhere in that graph fails the build with
 * `UnhandledSchemeError`. `process.getBuiltinModule` (Node 22+, which every
 * image and dev environment here runs) gives the same access with no import, so
 * the module stays edge-safe and returns `null` — i.e. offline — where no
 * filesystem exists.
 */
function readTextFile(file: string): string | null {
  const getBuiltinModule = (
    process as unknown as { getBuiltinModule?: (id: string) => unknown }
  ).getBuiltinModule;
  if (typeof getBuiltinModule !== "function") return null;
  const fs = getBuiltinModule.call(process, "fs") as
    | { readFileSync?: (path: string, encoding: string) => string }
    | undefined;
  if (typeof fs?.readFileSync !== "function") return null;
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
}

function runtimeOverride(): boolean | null {
  const directory = configDirectory();
  if (!directory) return null;
  const raw = readTextFile(`${directory}/${FILE_NAME}`);
  if (raw === null) return null;
  try {
    const payload: unknown = JSON.parse(raw);
    const value = (payload as { offline_mode?: unknown } | null)?.offline_mode;
    return typeof value === "boolean" ? value : null;
  } catch {
    return null;
  }
}

export function isOfflineMode(): boolean {
  const override = runtimeOverride();
  if (override !== null) return override;
  return process.env.OFFLINE_MODE?.trim().toLowerCase() !== "false";
}

/** Every opt-in analytics integration, named so the UI and logs stay honest. */
export type AnalyticsSurface =
  | "datafast"
  | "tilly"
  | "posthog"
  | "posthog_session_recording"
  | "error_reporting";

export interface AnalyticsDecision {
  allowed: boolean;
  offline_mode: boolean;
  /** Surfaces that are configured but suppressed by offline mode. */
  suppressed: AnalyticsSurface[];
}

function configuredSurfaces(): AnalyticsSurface[] {
  const surfaces: AnalyticsSurface[] = [];
  if (process.env.NEXT_PUBLIC_DATAFAST_WEBSITE_ID?.trim() && process.env.NEXT_PUBLIC_DATAFAST_DOMAIN?.trim()) {
    surfaces.push("datafast");
  }
  if (process.env.NEXT_PUBLIC_TILLY_WEBSITE_ID?.trim()) surfaces.push("tilly");
  if (process.env.NEXT_PUBLIC_POSTHOG_KEY?.trim()) {
    surfaces.push("posthog", "posthog_session_recording", "error_reporting");
  }
  return surfaces;
}

/**
 * One answer for the whole app: analytics may run only when offline mode is
 * explicitly off. Turning offline mode off is itself a deliberate operator
 * action, which is the "opt-in" this project requires.
 */
export function analyticsDecision(): AnalyticsDecision {
  const offline = isOfflineMode();
  const configured = configuredSurfaces();
  return {
    allowed: !offline && configured.length > 0,
    offline_mode: offline,
    suppressed: offline ? configured : [],
  };
}

/** Convenience for call sites that only need the boolean. */
export function isAnalyticsAllowed(): boolean {
  return analyticsDecision().allowed;
}
