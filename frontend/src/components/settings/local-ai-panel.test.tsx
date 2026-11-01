import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LocalAiPanel } from "./local-ai-panel";

const BASE_STATUS = {
  app: "ClipWright Studio",
  offline_mode: true,
  offline_mode_source: "env",
  offline_mode_from_env: true,
  offline_mode_state_path: "/config/offline-mode.json",
  allow_hosted_ai: false,
  hosted_ai_available: false,
  local_only: true,
  ready_for_offline_processing: true,
  transcription: {
    provider: "whisper",
    whisper: {
      model: "small",
      path: "/models/whisper/small.pt",
      directory: "/models/whisper",
      available: true,
      size_mb: 466.2,
      expected_size_mb: 466,
      message: null,
    },
  },
  llm: {
    configured_model: "ollama:llama3.2:3b",
    provider: "ollama",
    model: "llama3.2:3b",
    local: true,
    reachable: true,
    model_available: true,
    installed_models: ["llama3.2:3b"],
    base_url: "http://localhost:11434",
    message: null,
    requires_internet: false,
  },
  runtime: { ffmpeg: { available: true, path: "/usr/bin/ffmpeg", message: null } },
  optional_online_features: [
    {
      id: "stream_downloads",
      label: "YouTube / Twitch / Kick downloads",
      requires_internet: true,
      detail: "Downloading a link needs the internet.",
    },
  ],
  checks: [
    { id: "whisper_model", label: "Whisper model (small)", ok: true, detail: "Ready" },
    { id: "ollama", label: "Ollama daemon", ok: true, detail: "Reachable" },
  ],
};

function jsonResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("LocalAiPanel", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(BASE_STATUS));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the backend's real status, including the offline switch", async () => {
    render(<LocalAiPanel />);

    expect(await screen.findByText("Ready for offline editing")).toBeInTheDocument();
    expect(screen.getByText(/Whisper · small/)).toBeInTheDocument();
    expect(screen.getByText(/Ollama · llama3.2:3b/)).toBeInTheDocument();
    expect(screen.getByLabelText("Offline mode")).toBeChecked();
    expect(screen.getByText(/No request, transcript, frame or prompt leaves this machine/)).toBeInTheDocument();
  });

  it("persists switching offline mode off and shows the saved state", async () => {
    const user = userEvent.setup();
    render(<LocalAiPanel />);
    await screen.findByText("Ready for offline editing");

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...BASE_STATUS, offline_mode: false, offline_mode_source: "runtime" }),
    );

    await user.click(screen.getByLabelText("Offline mode"));

    await waitFor(() => {
      expect(screen.getByLabelText("Offline mode")).not.toBeChecked();
    });
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/local-ai/offline-mode",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ offline_mode: false }),
      }),
    );
    // The .env default is still offered as a way back.
    expect(screen.getByRole("button", { name: /Use the .env default \(on\)/ })).toBeInTheDocument();
    expect(screen.getByText(/No hosted provider is enabled/)).toBeInTheDocument();
  });

  it("surfaces a backend error instead of pretending the switch worked", async () => {
    const user = userEvent.setup();
    render(<LocalAiPanel />);
    await screen.findByText("Ready for offline editing");

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: "The offline mode setting could not be saved." }, false, 503),
    );

    await user.click(screen.getByLabelText("Offline mode"));

    expect(await screen.findByText("The offline mode setting could not be saved.")).toBeInTheDocument();
    expect(screen.getByLabelText("Offline mode")).toBeChecked();
  });

  it("shows the setup error when the backend does not support the switch", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({ ...BASE_STATUS, checks: undefined }),
    );

    render(<LocalAiPanel />);

    expect(
      await screen.findByText(/This backend does not report local AI status yet/),
    ).toBeInTheDocument();
  });
});
