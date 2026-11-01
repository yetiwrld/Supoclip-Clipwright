import { NextResponse } from "next/server";

import { fetchBackend } from "@/server/backend-api";
import { getServerSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Persists the Offline Mode switch.
 *
 * The value is stored by the backend (a small JSON file on the mounted data
 * directory) so the API, the render worker and the AI worker all honour it and
 * the choice survives a restart. `offline_mode: null` clears the override and
 * falls back to the `.env` default.
 */
export async function POST(request: Request) {
  const session = await getServerSession();
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const payload = await request.json().catch(() => null);
  const value = payload?.offline_mode;
  if (value !== null && typeof value !== "boolean") {
    return NextResponse.json(
      { error: "offline_mode must be true, false, or null." },
      { status: 400 },
    );
  }

  try {
    const upstream = await fetchBackend("/local-ai/offline-mode", {
      method: "POST",
      cache: "no-store",
      userId: session.user.id,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ offline_mode: value }),
    });
    const body = await upstream.json().catch(() => null);
    if (!upstream.ok || !body) {
      return NextResponse.json(
        {
          error:
            body?.detail ||
            body?.error ||
            "The offline mode setting could not be saved. Is the backend running?",
        },
        { status: upstream.status === 200 ? 503 : upstream.status, headers: { "Cache-Control": "no-store" } },
      );
    }
    return NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    console.error("Failed to update offline mode:", error);
    return NextResponse.json(
      { error: "The offline mode setting could not be saved. Is the backend running?" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
