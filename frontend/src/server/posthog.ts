import { PostHog } from "posthog-node";

import { isOfflineMode } from "@/lib/offline-mode";
import { POSTHOG_HOST, POSTHOG_KEY, isPostHogEnabled } from "@/lib/posthog-config";

let client: PostHog | null = null;

/**
 * One client for the long-running Node server. `flushAt: 1` sends each event
 * straight away, so nothing waits in a queue if the container stops.
 */
export function getPostHogServer(): PostHog | null {
  // Offline mode: server errors and traces stay in the local logs. Checked on
  // every call (not cached) so the Settings switch applies immediately.
  if (!isPostHogEnabled || isOfflineMode()) return null;
  client ??= new PostHog(POSTHOG_KEY, { host: POSTHOG_HOST, flushAt: 1, flushInterval: 0 });
  return client;
}

/** The browser SDK keeps `{ distinct_id, ... }` in the `ph_<key>_posthog` cookie. */
export function distinctIdFromCookie(cookieHeader: string | undefined): string | undefined {
  if (!cookieHeader || !isPostHogEnabled) return undefined;
  const name = `ph_${POSTHOG_KEY}_posthog=`;
  const raw = cookieHeader
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(name))
    ?.slice(name.length);
  if (!raw) return undefined;
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(raw));
    const distinctId = (parsed as { distinct_id?: unknown } | null)?.distinct_id;
    return typeof distinctId === "string" && distinctId ? distinctId : undefined;
  } catch {
    return undefined;
  }
}
