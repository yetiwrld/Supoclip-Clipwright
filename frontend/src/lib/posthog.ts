import posthog from "posthog-js";

import { isClientAnalyticsAllowed } from "@/lib/analytics-gate";
import { isPostHogEnabled } from "@/lib/posthog-config";

export { isPostHogEnabled };

/** True only when a key exists *and* the server allowed analytics this request. */
export function isClientAnalyticsEnabled() {
  return isPostHogEnabled && isClientAnalyticsAllowed();
}

export function resetAnalytics() {
  if (isClientAnalyticsEnabled()) posthog.reset();
}

export function captureApiFailure(details: { method: string; path: string; status: number; traceId: string }) {
  if (!isClientAnalyticsEnabled()) return;
  posthog.captureException(new Error(`${details.method} ${details.path} failed with ${details.status}`), {
    trace_id: details.traceId,
    status: details.status,
    path: details.path,
    method: details.method,
  });
}
