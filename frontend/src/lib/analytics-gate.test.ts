import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ANALYTICS_ATTR, isClientAnalyticsAllowed } from "./analytics-gate";

describe("client analytics gate", () => {
  afterEach(() => {
    delete document.documentElement.dataset.analytics;
  });

  it("blocks analytics when the server rendered no marker (offline default)", () => {
    expect(document.documentElement.hasAttribute(ANALYTICS_ATTR)).toBe(false);
    expect(isClientAnalyticsAllowed()).toBe(false);
  });

  it("blocks analytics for any value except an explicit 'on'", () => {
    for (const value of ["off", "", "true", "1", "yes"]) {
      document.documentElement.setAttribute(ANALYTICS_ATTR, value);
      expect(isClientAnalyticsAllowed(), value).toBe(false);
    }
  });

  it("allows analytics only when the server explicitly opted in", () => {
    document.documentElement.setAttribute(ANALYTICS_ATTR, "on");
    expect(isClientAnalyticsAllowed()).toBe(true);
  });
});

describe("offline mode resolver", () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.resetModules();
  });

  it("is offline by default", async () => {
    delete process.env.OFFLINE_MODE;
    const { isOfflineMode } = await import("./offline-mode");
    expect(isOfflineMode()).toBe(true);
  });

  it("stays offline unless OFFLINE_MODE is exactly false", async () => {
    for (const value of ["TRUE", "1", "", "off"]) {
      process.env.OFFLINE_MODE = value;
      const { isOfflineMode } = await import("./offline-mode");
      expect(isOfflineMode(), value).toBe(true);
    }
  });

  it("reads the runtime switch written from Settings", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clipwright-config-"));
    fs.writeFileSync(path.join(dir, "offline-mode.json"), JSON.stringify({ offline_mode: false }));
    process.env.CLIPWRIGHT_CONFIG_DIR = dir;
    process.env.OFFLINE_MODE = "true";

    const { isOfflineMode } = await import("./offline-mode");
    // The Settings switch wins over the .env default.
    expect(isOfflineMode()).toBe(false);
  });

  it("fails closed when the runtime file is unreadable", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clipwright-config-"));
    fs.writeFileSync(path.join(dir, "offline-mode.json"), "{ not json");
    process.env.CLIPWRIGHT_CONFIG_DIR = dir;

    const { isOfflineMode } = await import("./offline-mode");
    expect(isOfflineMode()).toBe(true);
  });

  it("reports analytics as allowed only when offline mode is off and something is configured", async () => {
    process.env.OFFLINE_MODE = "true";
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test";
    let mod = await import("./offline-mode");
    let decision = mod.analyticsDecision();
    expect(decision.allowed).toBe(false);
    expect(decision.suppressed).toContain("posthog");

    vi.resetModules();
    process.env.OFFLINE_MODE = "false";
    mod = await import("./offline-mode");
    decision = mod.analyticsDecision();
    expect(decision.allowed).toBe(true);
    expect(decision.suppressed).toEqual([]);
  });
});
