"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  CircuitBoard,
  Cpu,
  Globe,
  Loader2,
  RefreshCw,
  Terminal,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

/**
 * Live local-AI status for Settings.
 *
 * Every value is fetched from the backend's `/local-ai/status` probe, which
 * asks Ollama for its model list, looks for the Whisper checkpoint on disk and
 * checks for FFmpeg. Nothing here is optimistic: if a model is missing, the row
 * is amber/red and the fix is spelled out.
 */
interface LocalAiCheck {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

interface LocalAiStatus {
  app: string;
  offline_mode: boolean;
  /** "env" when .env decides, "runtime" when switched from this screen. */
  offline_mode_source?: string;
  offline_mode_from_env?: boolean;
  offline_mode_state_path?: string;
  allow_hosted_ai: boolean;
  hosted_ai_available?: boolean;
  local_only?: boolean;
  ready_for_offline_processing: boolean;
  transcription: {
    provider: string;
    whisper: {
      model: string;
      path: string;
      directory: string;
      available: boolean;
      size_mb: number;
      expected_size_mb: number | null;
      message: string | null;
    };
  };
  llm: {
    configured_model: string;
    provider: string | null;
    model: string | null;
    local: boolean;
    reachable: boolean;
    model_available: boolean;
    installed_models: string[];
    base_url?: string;
    message: string | null;
    requires_internet: boolean;
  };
  runtime: { ffmpeg: { available: boolean; path: string; message: string | null } };
  optional_online_features: {
    id: string;
    label: string;
    requires_internet: boolean;
    configured?: boolean;
    detail: string;
  }[];
  checks: LocalAiCheck[];
}

export function LocalAiPanel() {
  const [status, setStatus] = useState<LocalAiStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingOfflineMode, setSavingOfflineMode] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/local-ai/status", { cache: "no-store" });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body) {
        setError(body?.error || "Local AI status is unavailable.");
        setStatus(null);
        return;
      }
      // Defensive: an older backend without /local-ai/status must not crash the
      // settings page — it shows the error state instead.
      if (!Array.isArray(body.checks) || !Array.isArray(body.optional_online_features)) {
        setError("This backend does not report local AI status yet. Rebuild the backend service.");
        setStatus(null);
        return;
      }
      setStatus(body as LocalAiStatus);
    } catch {
      setError("Could not reach the backend. Start the stack and refresh.");
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const ready = Boolean(status?.ready_for_offline_processing);

  /**
   * Persist the switch in the backend so every process (API, render worker, AI
   * worker) honours it and it survives a restart. Passing `null` clears the
   * override and returns to the `.env` default.
   */
  const updateOfflineMode = useCallback(async (next: boolean | null) => {
    setSavingOfflineMode(true);
    setError(null);
    try {
      const response = await fetch("/api/local-ai/offline-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offline_mode: next }),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body) {
        setError(body?.error || "The offline mode setting could not be saved.");
        return;
      }
      if (!Array.isArray(body.checks) || !Array.isArray(body.optional_online_features)) {
        setError("This backend does not support the offline mode switch yet. Rebuild the backend service.");
        return;
      }
      setStatus(body as LocalAiStatus);
    } catch {
      setError("Could not reach the backend. Start the stack and try again.");
    } finally {
      setSavingOfflineMode(false);
    }
  }, []);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
              loading
                ? "border-border text-muted-foreground"
                : ready
                  ? "border-success/30 bg-success/10 text-success"
                  : "border-warning/30 bg-warning/10 text-warning",
            )}
            aria-live="polite"
          >
            {loading ? (
              <Loader2 className="size-3 animate-spin" />
            ) : ready ? (
              <Check className="size-3" />
            ) : (
              <AlertTriangle className="size-3" />
            )}
            {loading ? "Checking local models…" : ready ? "Ready for offline editing" : "Setup needed"}
          </span>
          {status && (
            <span className="text-xs text-muted-foreground">
              {status.offline_mode ? "Offline mode on" : "Offline mode off"}
              {status.offline_mode_source === "runtime" ? " · set here" : " · from .env"}
              {status.hosted_ai_available ? " · hosted AI allowed" : ""}
            </span>
          )}
        </div>
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading} className="rounded-full">
          {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
          Re-check
        </Button>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
          <X className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {status && (
        <>
          <div className="rounded-xl border bg-card px-3.5 py-3">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <label
                  htmlFor="offline-mode-switch"
                  className="flex items-center gap-1.5 text-sm font-medium"
                >
                  <Globe className="size-3.5 text-muted-foreground" /> Offline mode
                </label>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  {status.offline_mode
                    ? "On. No request, transcript, frame or prompt leaves this machine, and hosted AI is refused."
                    : "Off. Hosted providers may be used only where one is configured and allowed."}
                  {status.offline_mode_source === "runtime"
                    ? " Saved from this screen and stored in the data directory, so it survives a restart."
                    : " Following the OFFLINE_MODE value in .env until you change it here."}
                </p>
                {!status.offline_mode && !status.hosted_ai_available && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    No hosted provider is enabled: local Whisper and Ollama still do all the work.
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2 pt-0.5">
                {savingOfflineMode && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
                <Switch
                  id="offline-mode-switch"
                  checked={status.offline_mode}
                  disabled={savingOfflineMode}
                  onCheckedChange={(checked) => void updateOfflineMode(checked)}
                  aria-label="Offline mode"
                />
              </div>
            </div>
            {status.offline_mode_source === "runtime" && status.offline_mode_from_env !== undefined && (
              <button
                type="button"
                className="mt-2 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground disabled:opacity-50"
                disabled={savingOfflineMode}
                onClick={() => void updateOfflineMode(null)}
              >
                Use the .env default ({status.offline_mode_from_env ? "on" : "off"})
              </button>
            )}
          </div>

          <ul className="divide-y overflow-hidden rounded-xl border">
            {status.checks.map((check) => (
              <li key={check.id} className="flex items-start gap-3 bg-card px-3.5 py-3">
                <StatusDot ok={check.ok} />
                <div className="min-w-0">
                  <p className="text-sm font-medium">{check.label}</p>
                  <p className="mt-0.5 break-words text-xs leading-relaxed text-muted-foreground">{check.detail}</p>
                </div>
              </li>
            ))}
          </ul>

          <dl className="grid gap-3 sm:grid-cols-2">
            <InfoTile
              icon={<Cpu className="size-3.5" />}
              label="Transcription"
              value={`Whisper · ${status.transcription.whisper.model}`}
              hint={
                status.transcription.whisper.available
                  ? `${status.transcription.whisper.size_mb} MB on disk`
                  : status.transcription.whisper.expected_size_mb
                    ? `~${status.transcription.whisper.expected_size_mb} MB once installed`
                    : "Not installed"
              }
            />
            <InfoTile
              icon={<CircuitBoard className="size-3.5" />}
              label="Analysis & AI editing"
              value={status.llm.local ? `Ollama · ${status.llm.model ?? "not set"}` : status.llm.configured_model}
              hint={
                status.llm.local
                  ? status.llm.reachable
                    ? status.llm.base_url
                    : "Ollama is not running"
                  : "Hosted provider"
              }
            />
          </dl>

          {status.llm.installed_models.length > 0 && (
            <p className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Ollama models installed: </span>
              {status.llm.installed_models.join(", ")}
            </p>
          )}

          {!ready && (
            <div className="rounded-xl border border-warning/30 bg-warning/5 px-3.5 py-3 text-xs">
              <p className="flex items-center gap-1.5 font-medium text-warning">
                <Terminal className="size-3.5" /> Fix it in one command
              </p>
              <code className="mt-2 block overflow-x-auto rounded-lg bg-background/70 px-2.5 py-2 font-mono text-[11px] text-foreground">
                python scripts/setup_local_ai.py --install-whisper --pull-ollama-model
              </code>
              <p className="mt-2 leading-relaxed text-muted-foreground">
                Models are stored on disk and reused after a restart. They are never downloaded during a
                processing job, so an offline machine is never asked for the network.
              </p>
            </div>
          )}

          <div>
            <p className="flex items-center gap-1.5 text-xs font-medium">
              <Globe className="size-3.5 text-muted-foreground" /> Features that need the internet
            </p>
            <ul className="mt-2 space-y-1.5">
              {status.optional_online_features.map((feature) => (
                <li key={feature.id} className="flex items-start justify-between gap-3 text-xs">
                  <span className="text-muted-foreground">
                    <span className="text-foreground">{feature.label}</span> — {feature.detail}
                  </span>
                  <span className="shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                    online only
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted-foreground">
              Everything else — importing local files, transcribing, clip selection, captions, the editor and
              exports — runs entirely on this machine.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "mt-1 size-2 shrink-0 rounded-full",
        ok ? "bg-success shadow-[0_0_0_3px] shadow-success/15" : "bg-warning shadow-[0_0_0_3px] shadow-warning/15",
      )}
    />
  );
}

function InfoTile({
  icon,
  label,
  value,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border bg-card px-3.5 py-3">
      <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </dt>
      <dd className="mt-1 text-sm font-medium">{value}</dd>
      {hint && <dd className="mt-0.5 break-words text-[11px] text-muted-foreground">{hint}</dd>}
    </div>
  );
}
