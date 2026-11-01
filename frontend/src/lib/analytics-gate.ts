/**
 * Client-side mirror of the server's analytics decision.
 *
 * The server layout renders `data-analytics="on"` on <html> only when offline
 * mode is off *and* an integration is configured. Every browser-side call site
 * (PostHog init, error capture, DataFast/Tilly goals) checks this attribute, so
 * a page that was server-rendered in offline mode can never start sending
 * events — even if a stale bundle is cached, or an operator flips the switch
 * while a tab is open and then reloads.
 *
 * Absence of the attribute means "do not track". No attribute, no analytics.
 */

export const ANALYTICS_ATTR = "data-analytics";

export function isClientAnalyticsAllowed(): boolean {
  if (typeof document === "undefined") return false;
  return document.documentElement?.getAttribute(ANALYTICS_ATTR) === "on";
}
