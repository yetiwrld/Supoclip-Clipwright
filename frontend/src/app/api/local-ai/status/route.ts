import { NextResponse } from "next/server";

import { fetchBackend } from "@/server/backend-api";
import { getServerSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Proxies the backend's live local-AI probe to the settings screen.
 *
 * Every field comes from the backend (Ollama HTTP probe, Whisper checkpoint on
 * disk, FFmpeg lookup); nothing here is cached or hardcoded so the UI cannot
 * show a green dot while a model is missing.
 */
export async function GET() {
  const session = await getServerSession();
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const upstream = await fetchBackend("/local-ai/status", {
      cache: "no-store",
      userId: session.user.id,
    });
    const body = await upstream.json().catch(() => null);
    if (!upstream.ok || !body) {
      return NextResponse.json(
        { error: "Local AI status is unavailable. Is the backend running?" },
        { status: upstream.status === 200 ? 503 : upstream.status, headers: { "Cache-Control": "no-store" } },
      );
    }
    return NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    console.error("Failed to load local AI status:", error);
    return NextResponse.json(
      { error: "Local AI status is unavailable. Is the backend running?" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
