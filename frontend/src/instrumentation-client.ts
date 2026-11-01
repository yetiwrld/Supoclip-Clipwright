import posthog from "posthog-js";

import { isClientAnalyticsAllowed } from "@/lib/analytics-gate";
import { POSTHOG_KEY, isPostHogEnabled, posthogUiHost } from "@/lib/posthog-config";
import { installTraceFetch } from "@/lib/trace-fetch";

// Sends nothing on its own; the error/trace reporting helpers are gated too.
installTraceFetch();

// Offline mode renders no analytics: the server omits `data-analytics="on"`, so
// PostHog is never initialised and session replay never starts. This is the
// runtime switch, not just the build-time key.
if (isPostHogEnabled && isClientAnalyticsAllowed()) {
  posthog.init(POSTHOG_KEY, {
    api_host: "/ingest",
    ui_host: posthogUiHost(),
    defaults: "2026-08-30",
    person_profiles: "identified_only",
    capture_exceptions: true,
    session_recording: {
      maskAllInputs: true,
    },
  });
  posthog.register({ platform: "web" });
}
