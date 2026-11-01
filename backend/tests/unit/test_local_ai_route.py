"""HTTP surface of the local-AI route: status + the Offline Mode switch."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src import local_ai, offline_mode
from src.api.routes import local_ai as local_ai_route
from src.config import Config


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPWRIGHT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_HOSTED_AI", "false")
    offline_mode.reset_cache()

    # No network, no auth plumbing: the guard is exercised elsewhere.
    monkeypatch.setattr(local_ai_route, "get_authenticated_user_id", lambda *a, **k: 1)
    monkeypatch.setattr(
        local_ai,
        "check_ollama",
        lambda config=None, timeout=None: {
            "base_url": "http://localhost:11434",
            "openai_base_url": "http://localhost:11434/v1",
            "reachable": True,
            "models": ["llama3.2:3b"],
            "error": None,
        },
    )
    monkeypatch.setattr(
        local_ai,
        "check_ffmpeg",
        lambda: {"available": True, "path": "/usr/bin/ffmpeg", "ffprobe_available": True, "message": None},
    )

    application = FastAPI()
    application.include_router(local_ai_route.router)
    yield application
    offline_mode.reset_cache()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_status_route_reports_offline_defaults(app):
    async with await _client(app) as client:
        response = await client.get("/local-ai/status")

    assert response.status_code == 200
    body = response.json()
    assert body["offline_mode"] is True
    assert body["offline_mode_source"] == "env"
    assert body["local_only"] is True
    assert body["app"] == "ClipWright Studio"


@pytest.mark.anyio
async def test_switch_off_persists_and_returns_fresh_status(app):
    async with await _client(app) as client:
        response = await client.post("/local-ai/offline-mode", json={"offline_mode": False})

    assert response.status_code == 200
    body = response.json()
    assert body["offline_mode"] is False
    assert body["offline_mode_source"] == "runtime"
    assert body["hosted_ai_available"] is False

    state = json.loads((offline_mode.state_path()).read_text())
    assert state == {"offline_mode": False}
    assert Config().offline_mode is False


@pytest.mark.anyio
async def test_null_clears_the_override(app):
    async with await _client(app) as client:
        await client.post("/local-ai/offline-mode", json={"offline_mode": False})
        response = await client.post("/local-ai/offline-mode", json={"offline_mode": None})

    assert response.status_code == 200
    assert response.json()["offline_mode"] is True
    assert response.json()["offline_mode_source"] == "env"
    assert not offline_mode.state_path().exists()


@pytest.mark.anyio
async def test_non_boolean_payload_is_rejected(app):
    async with await _client(app) as client:
        response = await client.post("/local-ai/offline-mode", json={"offline_mode": "off"})

    assert response.status_code == 422
